"""Provider seam for the interpretation generator.

A single `generate(model, prompt, *, temperature, seed, supports_temperature,
supports_seed)` function turns a prompt into text + token usage for any supported
provider, applying only the parameters the model's capability flags allow (the
caller reads those flags from the `models` table and passes them in; llm.py stays
DB-free). The canonical "provider:model" string is split at exactly one point — the
entry to `generate()`, via `split_model` — so the stored, selected, and priced
forms never drift apart.

No DB, no prompt-building, no dashboard here: callers (the Phase 2 generator core)
build the prompt and persist the result. Provider SDKs are imported lazily inside
each client factory so this module loads even when a given SDK is absent, and the
mocked test suite can patch the factory without touching the network.
"""

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from dotenv import load_dotenv

import config

# Load .env once at import so the key checks below read from a populated
# os.environ. Done here (not inside the per-call factories) so a test that
# deletes a key via monkeypatch.delenv is not silently undone by a reload.
load_dotenv()

# Supported providers, in the canonical order surfaced to the user. The first
# element of a canonical "provider:model" string must be one of these.
SUPPORTED_PROVIDERS = ("anthropic", "openai", "xai", "google", "ollama")

# Providers whose adapter actually HONORS the per-run `think` toggle (sends it to
# the model and captures the thinking output). This is the second gate, distinct
# from a model's is_reasoning capability: a model can be reasoning-capable yet run
# on a provider that does not expose think on/off (e.g. OpenAI's reasoning is
# internal). generate() forwards think only for a provider in this set, and the UI
# uses it to decide whether the think toggle is interactive or shown-but-disabled.
THINK_PROVIDERS = frozenset({"ollama"})

# The output cap is no longer a flat constant: it is the per-model max_tokens column
# (config.DEFAULT_MAX_TOKENS / DEFAULT_MAX_TOKENS_REASONING seed the rows), threaded
# through generate() into each adapter and omitted entirely when NULL (uncapped).

# Whether a model honors temperature / seed is no longer inferred from the model
# id here: it is read from the `models` table (supports_temperature / supports_seed)
# and threaded into generate() as flags. The adapters keep only the provider-level
# rules (temperature ranges, Anthropic's structural no-seed). gpt-5.4-nano, e.g., is
# seeded supports_temperature=0 (reasoning models reject a non-default temperature).


class LLMError(Exception):
    """Raised for provider-seam failures: a missing API key, an unknown provider,
    an out-of-range temperature, or a malformed model string. A plain Exception
    (not ConfigError) — these are runtime provider failures, distinct from config
    validation — so the Phase 3 endpoint can catch it and return a clean error
    payload instead of a 500. Messages name the offending provider."""


@dataclass
class GenerateResult:
    """The normalized result of one generation, identical in shape across
    providers so callers never branch on provider. `seed_applied` is the seed
    that actually governed generation (the value sent to the provider), or None
    when the provider did not apply one.

    `thinking` and `think_applied` are the cross-provider think fields: `thinking`
    is the reasoning CONTENT a thinking model produced (for display), and
    `think_applied` is the applied 0/1 toggle value (for persistence), mirroring
    seed_applied. Both are None for every adapter that does not honor think (the
    four cloud adapters always leave them None); only a THINK_PROVIDERS adapter
    populates them. think_applied stays None when think did not apply at all."""

    text: str
    input_tokens: int
    output_tokens: int
    seed_applied: int | None
    thinking: str | None = None
    think_applied: int | None = None


def split_model(s: str) -> tuple[str, str]:
    """Split a canonical "provider:model" string into (provider, model) on the
    FIRST colon, so a model id that itself contains a colon survives. This is the
    ONE place the combined string is split; `generate` is the only caller. A
    string with no colon raises LLMError."""
    provider, sep, model = s.partition(":")
    if not sep or not provider or not model:
        raise LLMError(
            f"Malformed model string {s!r}; expected canonical 'provider:model'"
        )
    return provider, model


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  prices: dict) -> float | None:
    """Compute the USD cost estimate for a generation from a `[prices]` map keyed
    by the canonical combined "provider:model" string. Returns None when the model
    is absent from the map (the "unavailable" display path). Pure — no I/O. Prices
    are USD per 1M tokens."""
    entry = prices.get(model)
    if entry is None:
        return None
    return (input_tokens / 1_000_000 * entry["input"]
            + output_tokens / 1_000_000 * entry["output"])


def generate(model: str, prompt: str, *, temperature: float,
             seed: int | None, supports_temperature: bool,
             supports_seed: bool, think: bool | None = None,
             is_reasoning: bool = False,
             max_tokens: int | None = None) -> GenerateResult:
    """Generate a summary for `prompt` using the canonical combined `model`
    string. Splits the model exactly here, dispatches to the matching provider
    adapter, and returns a normalized GenerateResult. An unknown provider raises
    LLMError listing the supported set.

    `supports_temperature` / `supports_seed` / `is_reasoning` are the per-model
    capability flags (read from the `models` table by the caller, which holds the DB
    connection — llm.py stays DB-free). They gate parameter forwarding: a model whose
    row says no-temperature omits it (the gpt-5 reasoning case), and no-seed drops it
    here so no adapter ever forwards one. `think` is the per-run reasoning toggle,
    forwarded ONLY when the model is reasoning-capable AND its provider honors think
    (THINK_PROVIDERS); otherwise it is normalized to None here so no adapter receives
    it for a model it cannot apply it to (and the persisted think_applied is None).
    Provider-level rules (temperature range checks, Anthropic's structural no-seed)
    stay in the adapters, independent of the flag.

    `max_tokens` is the per-model output cap (read from the same row by the caller).
    None means uncapped: each adapter omits its cap parameter, exactly as it omits an
    unset temperature/seed. It defaults to None so a row-less caller (e.g. the live
    xAI verification) runs without forwarding a cap. The provider enforces its own
    hard ceiling; an over-ceiling value surfaces through the adapter's LLMError."""
    provider, model_id = split_model(model)
    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        supported = ", ".join(SUPPORTED_PROVIDERS)
        raise LLMError(
            f"Unknown provider {provider!r}; supported providers are: {supported}"
        )
    # A non-positive seed is treated as "no seed". xAI rejects seed <= 0 ("Seed must
    # be positive"); 0/negative are degenerate elsewhere too. Normalizing here (one
    # place) keeps behavior uniform and forwards only a valid seed; seed_applied then
    # logs None for these, the honest "no seed governed this run".
    if seed is not None and seed <= 0:
        seed = None
    # A model whose row does not support a seed never forwards one, whatever the
    # caller passed. Anthropic also drops it structurally in its adapter.
    if not supports_seed:
        seed = None
    # think applies only when the model can reason AND this provider honors the
    # toggle. Otherwise it is "not applicable": drop it to None here (the one place),
    # so the adapter never sees think for a model it cannot apply it to and the
    # adapter records think_applied = None. A genuine False (Ollama, toggled off)
    # survives this — it is NOT collapsed to None, so an off-run persists 0, not NULL.
    if not (is_reasoning and provider in THINK_PROVIDERS):
        think = None
    return adapter(model_id, prompt, temperature=temperature, seed=seed,
                   supports_temperature=supports_temperature, think=think,
                   max_tokens=max_tokens)


def _api_error_base(provider: str):
    """The installed SDK's API-error base class for `provider`, imported lazily
    (the SDK is only present/needed when the adapter runs). Adapters catch this to
    convert a provider API failure (bad request, model not found, rate limit, auth,
    connection) into a clean LLMError, so the dashboard shows an inline message
    instead of a raw 500. Catching the BASE (not bare Exception) means genuine code
    bugs still surface as 500. Verified against installed versions: openai 2.41.0
    (OpenAIError), anthropic 0.109.1 (AnthropicError), google.genai (errors.APIError).
    xAI rides the openai client, so its errors are in the openai hierarchy."""
    if provider in ("openai", "xai"):
        import openai
        return openai.OpenAIError
    if provider == "anthropic":
        import anthropic
        return anthropic.AnthropicError
    from google.genai import errors
    return errors.APIError


# --- key + temperature helpers ---------------------------------------------

def _require_key(env_var: str, provider: str) -> str:
    """Return the API key from os.environ, or raise a provider-named LLMError.
    Reads only — .env is loaded once at module import, never here — so a missing
    key always surfaces as a catchable LLMError."""
    key = os.getenv(env_var)
    if not key:
        raise LLMError(f"missing {env_var} for {provider}")
    return key


def _check_temperature(provider: str, temperature: float,
                       low: float, high: float) -> None:
    """Raise a clear, provider-named LLMError if temperature is outside the
    range this provider accepts."""
    if not (low <= temperature <= high):
        raise LLMError(
            f"temperature {temperature} out of range [{low}, {high}] "
            f"for {provider}"
        )


# --- client factories (key check FIRST, then lazy SDK import) ---------------
# Order is load-bearing: read the key and raise before importing the SDK, so an
# absent key never surfaces as an ImportError. Each is a separate seam tests
# patch to inject a fake client.

def _client_anthropic():
    key = _require_key("ANTHROPIC_API_KEY", "anthropic")
    import anthropic
    return anthropic.Anthropic(api_key=key)


def _client_openai():
    key = _require_key("OPENAI_API_KEY", "openai")
    import openai
    return openai.OpenAI(api_key=key)


def _client_xai():
    key = _require_key("XAI_API_KEY", "xai")
    import openai
    return openai.OpenAI(api_key=key, base_url="https://api.x.ai/v1")


def _client_google():
    key = _require_key("GEMINI_API_KEY", "google")
    from google import genai
    return genai.Client(api_key=key)


# --- adapters ---------------------------------------------------------------
# Each normalizes its provider's response into GenerateResult, applying only the
# parameters that provider honors, and reports seed_applied = the seed that
# actually governed generation (None when the provider applied none).

def _generate_anthropic(model_id, prompt, *, temperature, seed, supports_temperature,
                        think=None, max_tokens=None):
    # Anthropic Messages API: temperature 0.0-1.0, forwarded when the model's flag
    # allows. seed is NOT a parameter here, so it is omitted STRUCTURALLY (regardless
    # of supports_seed) and seed_applied is always None. `think` is accepted for a
    # uniform adapter contract but never honored (anthropic is not in THINK_PROVIDERS,
    # so generate() always passes None) → thinking/think_applied stay None. max_tokens
    # is the per-model output cap, forwarded only when set (None = uncapped, omitted).
    client = _client_anthropic()
    kwargs = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if supports_temperature:
        _check_temperature("anthropic", temperature, 0.0, 1.0)
        kwargs["temperature"] = temperature
    try:
        resp = client.messages.create(**kwargs)
    except _api_error_base("anthropic") as e:
        raise LLMError(f"anthropic: {e}") from e
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return GenerateResult(text, resp.usage.input_tokens,
                          resp.usage.output_tokens, seed_applied=None,
                          thinking=None,
                          think_applied=(None if think is None else int(think)))


def _generate_openai(model_id, prompt, *, temperature, seed, supports_temperature,
                     think=None, max_tokens=None):
    # supports_temperature is False for reasoning models (GPT-5 / o-series), which
    # reject any non-default temperature → omit it (not forwarded, not validated).
    # Others accept 0.0-2.0 → validate and forward. seed IS supported (verified live
    # on gpt-5.4-nano); the cap param is max_completion_tokens, forwarded only when
    # set (None = uncapped, omitted).
    client = _client_openai()
    kwargs = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
    }
    if max_tokens is not None:
        kwargs["max_completion_tokens"] = max_tokens
    if supports_temperature:
        _check_temperature("openai", temperature, 0.0, 2.0)
        kwargs["temperature"] = temperature
    if seed is not None:
        kwargs["seed"] = seed
    try:
        resp = client.chat.completions.create(**kwargs)
    except _api_error_base("openai") as e:
        raise LLMError(f"openai: {e}") from e
    return GenerateResult(resp.choices[0].message.content,
                          resp.usage.prompt_tokens, resp.usage.completion_tokens,
                          seed_applied=seed, thinking=None,
                          think_applied=(None if think is None else int(think)))


def _generate_xai(model_id, prompt, *, temperature, seed, supports_temperature,
                  think=None, max_tokens=None):
    # xAI is OpenAI-compatible: temperature 0.0-2.0 forwarded when supported, seed
    # honored (best-effort), standard max_tokens cap forwarded only when set
    # (None = uncapped, omitted).
    client = _client_xai()
    kwargs = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if supports_temperature:
        _check_temperature("xai", temperature, 0.0, 2.0)
        kwargs["temperature"] = temperature
    if seed is not None:
        kwargs["seed"] = seed
    try:
        resp = client.chat.completions.create(**kwargs)
    except _api_error_base("xai") as e:
        raise LLMError(f"xai: {e}") from e
    return GenerateResult(resp.choices[0].message.content,
                          resp.usage.prompt_tokens, resp.usage.completion_tokens,
                          seed_applied=seed, thinking=None,
                          think_applied=(None if think is None else int(think)))


def _generate_google(model_id, prompt, *, temperature, seed, supports_temperature,
                     think=None, max_tokens=None):
    # Gemini: temperature 0.0-2.0 and seed both ride in GenerateContentConfig, each
    # set ONLY when applicable; usage lives under
    # usage_metadata.{prompt,candidates}_token_count. max_output_tokens is the cap,
    # set only when max_tokens is provided (None = uncapped, omitted).
    client = _client_google()
    from google.genai import types
    cfg_kwargs = {}
    if max_tokens is not None:
        cfg_kwargs["max_output_tokens"] = max_tokens
    if supports_temperature:
        _check_temperature("google", temperature, 0.0, 2.0)
        cfg_kwargs["temperature"] = temperature
    if seed is not None:
        cfg_kwargs["seed"] = seed
    config = types.GenerateContentConfig(**cfg_kwargs)
    try:
        resp = client.models.generate_content(
            model=model_id, contents=prompt, config=config
        )
    except _api_error_base("google") as e:
        raise LLMError(f"google: {e}") from e
    usage = resp.usage_metadata
    return GenerateResult(resp.text, usage.prompt_token_count,
                          usage.candidates_token_count, seed_applied=seed,
                          thinking=None,
                          think_applied=(None if think is None else int(think)))


# --- Ollama: local HTTP, no SDK, no API key ---------------------------------

def _ollama_request(payload: dict) -> dict:
    """POST `payload` to the configured local Ollama /api/generate endpoint and
    return the parsed JSON response. The ONE network seam for the Ollama adapter,
    isolated so tests patch it without a socket. Reads the base URL + timeout from
    config (no literal here). Lets urllib/OS/JSON errors propagate; the adapter wraps
    them as LLMError."""
    url = config.OLLAMA_BASE_URL.rstrip("/") + "/api/generate"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=config.OLLAMA_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


def _generate_ollama(model_id, prompt, *, temperature, seed, supports_temperature,
                     think=None, max_tokens=None):
    # max_tokens is accepted for the uniform adapter contract but deliberately IGNORED
    # (no num_predict): Ollama is local and free, so the cost ceiling that justifies a
    # cap elsewhere does not apply and runaway runtime is the timeout's job. Its row is
    # NULL (uncapped) and never reaches here as a real cap.
    # Local Ollama over HTTP: no SDK, no API key, base URL + timeout from config.
    # model_id is the bare tag (split_model already stripped the 'ollama:' prefix).
    # temperature/seed ride in `options` only when applicable; NO temperature range
    # literal here — Phase 0 verified the default and that it is honored, not a valid
    # range, so we forward it and let Ollama bound it (a rejection surfaces via the
    # error wrap). `think` is a top-level toggle, non-None only when generate() left
    # it so (reasoning-capable model on a think-honoring provider). When think is on,
    # the response carries a separate `thinking` field and eval_count already includes
    # the generated thinking tokens, so output_tokens covers them.
    options = {}
    if supports_temperature:
        options["temperature"] = temperature
    if seed is not None:
        options["seed"] = seed
    payload = {"model": model_id, "prompt": prompt, "stream": False}
    if options:
        payload["options"] = options
    if think is not None:
        payload["think"] = bool(think)
    try:
        resp = _ollama_request(payload)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        # A not-running / not-pulled / timed-out server, a non-200 (HTTPError ⊂
        # URLError), or a malformed body all become a clean inline error, never a 500.
        raise LLMError(f"ollama: {e}") from e
    # Defensive parse against the evidenced response shape (a real stream:false
    # /api/generate body): response:str, thinking:str (present only with think on),
    # prompt_eval_count:int, eval_count:int. A missing / renamed / mistyped key is a
    # clean LLMError, never a KeyError 500.
    text = resp.get("response")
    input_tokens = resp.get("prompt_eval_count")
    output_tokens = resp.get("eval_count")
    if (not isinstance(text, str)
            or not isinstance(input_tokens, int) or isinstance(input_tokens, bool)
            or not isinstance(output_tokens, int) or isinstance(output_tokens, bool)):
        raise LLMError(
            "ollama: malformed response (expected str response and int "
            f"prompt_eval_count/eval_count); got keys {sorted(resp)}"
        )
    thinking = resp.get("thinking") if think else None
    return GenerateResult(text, input_tokens, output_tokens, seed_applied=seed,
                          thinking=thinking,
                          think_applied=(None if think is None else int(think)))


_ADAPTERS = {
    "anthropic": _generate_anthropic,
    "openai": _generate_openai,
    "xai": _generate_xai,
    "google": _generate_google,
    "ollama": _generate_ollama,
}
