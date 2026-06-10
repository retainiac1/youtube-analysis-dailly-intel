"""Provider seam for the interpretation generator.

A single `generate(model, prompt, *, temperature, seed)` function turns a prompt
into text + token usage for any supported provider, applying only the parameters
that provider actually honors. The canonical "provider:model" string is split at
exactly one point — the entry to `generate()`, via `split_model` — so the stored,
selected, and priced forms never drift apart.

No DB, no prompt-building, no dashboard here: callers (the Phase 2 generator core)
build the prompt and persist the result. Provider SDKs are imported lazily inside
each client factory so this module loads even when a given SDK is absent, and the
mocked test suite can patch the factory without touching the network.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load .env once at import so the key checks below read from a populated
# os.environ. Done here (not inside the per-call factories) so a test that
# deletes a key via monkeypatch.delenv is not silently undone by a reload.
load_dotenv()

# Supported providers, in the canonical order surfaced to the user. The first
# element of a canonical "provider:model" string must be one of these.
SUPPORTED_PROVIDERS = ("anthropic", "openai", "xai", "google")

# Output cap for a 3-4 sentence summary. Generous enough that the model is never
# truncated mid-sentence; small enough to keep cost and latency low.
MAX_OUTPUT_TOKENS = 512

# OpenAI reasoning families (GPT-5 series, o-series) reject any non-default
# temperature; gpt-5.4-nano is the current price-map instance. A model id
# beginning with one of these prefixes is treated as reasoning → temperature is
# omitted (not forwarded, not range-validated). Non-reasoning OpenAI models
# (gpt-4o / gpt-4.1 class) DO accept temperature 0.0-2.0, so it is forwarded and
# validated. (A temperature-supporting gpt-5-*-chat variant would be the
# exception; none is in the price map today — add a carve-out here if one is.)
OPENAI_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")


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
    when the provider did not apply one."""

    text: str
    input_tokens: int
    output_tokens: int
    seed_applied: int | None


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
             seed: int | None) -> GenerateResult:
    """Generate a summary for `prompt` using the canonical combined `model`
    string. Splits the model exactly here, dispatches to the matching provider
    adapter (which applies only the parameters that provider honors), and returns
    a normalized GenerateResult. An unknown provider raises LLMError listing the
    supported set."""
    provider, model_id = split_model(model)
    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        supported = ", ".join(SUPPORTED_PROVIDERS)
        raise LLMError(
            f"Unknown provider {provider!r}; supported providers are: {supported}"
        )
    return adapter(model_id, prompt, temperature=temperature, seed=seed)


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


def _is_openai_reasoning_model(model_id: str) -> bool:
    """True for OpenAI reasoning models (GPT-5 / o-series), which reject any
    non-default temperature. Used to scope the temperature omission to those
    models rather than all of OpenAI."""
    return model_id.startswith(OPENAI_REASONING_PREFIXES)


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

def _generate_anthropic(model_id, prompt, *, temperature, seed):
    # Anthropic Messages API: temperature 0.0-1.0; seed is not a parameter, so it
    # is omitted and seed_applied is always None.
    _check_temperature("anthropic", temperature, 0.0, 1.0)
    client = _client_anthropic()
    resp = client.messages.create(
        model=model_id,
        max_tokens=MAX_OUTPUT_TOKENS,
        temperature=temperature,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return GenerateResult(text, resp.usage.input_tokens,
                          resp.usage.output_tokens, seed_applied=None)


def _generate_openai(model_id, prompt, *, temperature, seed):
    # Reasoning models (GPT-5 / o-series) reject any non-default temperature, so
    # for those it is omitted (not forwarded, not range-validated). Non-reasoning
    # models accept temperature 0.0-2.0 → validate and forward. seed IS supported
    # (verified live on gpt-5.4-nano); the cap param is max_completion_tokens.
    client = _client_openai()
    kwargs = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
    }
    if not _is_openai_reasoning_model(model_id):
        _check_temperature("openai", temperature, 0.0, 2.0)
        kwargs["temperature"] = temperature
    if seed is not None:
        kwargs["seed"] = seed
    resp = client.chat.completions.create(**kwargs)
    return GenerateResult(resp.choices[0].message.content,
                          resp.usage.prompt_tokens, resp.usage.completion_tokens,
                          seed_applied=seed)


def _generate_xai(model_id, prompt, *, temperature, seed):
    # xAI is OpenAI-compatible: temperature 0.0-2.0 forwarded, seed honored
    # (best-effort), standard max_tokens cap.
    _check_temperature("xai", temperature, 0.0, 2.0)
    client = _client_xai()
    kwargs = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": MAX_OUTPUT_TOKENS,
    }
    if seed is not None:
        kwargs["seed"] = seed
    resp = client.chat.completions.create(**kwargs)
    return GenerateResult(resp.choices[0].message.content,
                          resp.usage.prompt_tokens, resp.usage.completion_tokens,
                          seed_applied=seed)


def _generate_google(model_id, prompt, *, temperature, seed):
    # Gemini: temperature 0.0-2.0 and seed both ride in GenerateContentConfig;
    # usage lives under usage_metadata.{prompt,candidates}_token_count.
    _check_temperature("google", temperature, 0.0, 2.0)
    client = _client_google()
    from google.genai import types
    config = types.GenerateContentConfig(
        temperature=temperature,
        seed=seed,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    resp = client.models.generate_content(
        model=model_id, contents=prompt, config=config
    )
    usage = resp.usage_metadata
    return GenerateResult(resp.text, usage.prompt_token_count,
                          usage.candidates_token_count, seed_applied=seed)


_ADAPTERS = {
    "anthropic": _generate_anthropic,
    "openai": _generate_openai,
    "xai": _generate_xai,
    "google": _generate_google,
}
