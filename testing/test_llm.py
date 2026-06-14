import urllib.error
from types import SimpleNamespace

import pytest

import config
import llm


# --- split_model -----------------------------------------------------------

def test_split_model_basic():
    assert llm.split_model("anthropic:claude-haiku-4-5") == (
        "anthropic", "claude-haiku-4-5"
    )


def test_split_model_splits_on_first_colon_only():
    # A model id that itself contains a colon must survive the split.
    assert llm.split_model("openai:gpt-5.4:nano") == ("openai", "gpt-5.4:nano")


def test_split_model_no_colon_raises():
    with pytest.raises(llm.LLMError, match="provider:model"):
        llm.split_model("noprovider")


# --- estimate_cost ---------------------------------------------------------

def test_estimate_cost_known_model():
    prices = {"openai:gpt-5.4-nano": {"input": 0.20, "output": 1.25}}
    # 3000/1e6*0.20 + 200/1e6*1.25
    cost = llm.estimate_cost("openai:gpt-5.4-nano", 3000, 200, prices)
    assert cost == pytest.approx(0.0006 + 0.00025)


def test_estimate_cost_unknown_model_returns_none():
    assert llm.estimate_cost("made:up", 1000, 1000, config.PRICES) is None


def test_estimate_cost_zero_tokens_known_model():
    prices = {"x:y": {"input": 1.0, "output": 5.0}}
    assert llm.estimate_cost("x:y", 0, 0, prices) == 0.0


def test_estimate_cost_uses_real_price_map():
    # Sanity against the shipped Phase 0 map: a sub-cent number, not None.
    cost = llm.estimate_cost("anthropic:claude-haiku-4-5", 1000, 1000, config.PRICES)
    assert cost is not None and cost > 0


# --- generate dispatch -----------------------------------------------------

def test_generate_unknown_provider_lists_supported_set():
    with pytest.raises(llm.LLMError) as exc:
        llm.generate("nope:model", "hi", temperature=0.5, seed=None,
                     supports_temperature=True, supports_seed=True)
    msg = str(exc.value)
    for provider in ("anthropic", "openai", "xai", "google"):
        assert provider in msg


# --- adapter fakes ---------------------------------------------------------
# Hand-built fakes in the house style (cf. FakeYouTube in test_search.py). Each
# records the kwargs of the completion call into `rec` and returns a canned
# response with that provider's usage-field shape.

def _fake_anthropic(rec, *, text="ok", input_tokens=11, output_tokens=7):
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )

    class _Messages:
        def create(self, **kwargs):
            rec.update(kwargs)
            return resp

    return SimpleNamespace(messages=_Messages())


def _fake_openai(rec, *, content="ok", prompt_tokens=11, completion_tokens=7):
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens,
                              completion_tokens=completion_tokens),
    )

    class _Completions:
        def create(self, **kwargs):
            rec.update(kwargs)
            return resp

    return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))


def _fake_google(rec, *, text="ok", prompt_token_count=11, candidates_token_count=7):
    resp = SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=prompt_token_count,
                                       candidates_token_count=candidates_token_count),
    )

    class _Models:
        def generate_content(self, **kwargs):
            rec.update(kwargs)
            return resp

    return SimpleNamespace(models=_Models())


def _fake_ollama(rec, payload, *, response="ok", thinking=None,
                 prompt_eval_count=11, eval_count=7):
    # The Ollama seam is _ollama_request(payload) -> dict, not a client factory:
    # record the POSTed payload and return a canned /api/generate body. `thinking`
    # is present only when the run requested think (mirrors the real API).
    rec.update(payload)
    body = {"response": response, "prompt_eval_count": prompt_eval_count,
            "eval_count": eval_count}
    if thinking is not None:
        body["thinking"] = thinking
    return body


# --- adapter happy paths + parameter forwarding ----------------------------

def test_generate_anthropic(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_anthropic",
                        lambda: _fake_anthropic(rec, text="hi", input_tokens=20,
                                                output_tokens=5))
    r = llm.generate("anthropic:claude-haiku-4-5", "p", temperature=0.5, seed=42,
                     supports_temperature=True, supports_seed=False)
    assert (r.text, r.input_tokens, r.output_tokens) == ("hi", 20, 5)
    assert r.seed_applied is None             # anthropic never applies a seed
    assert rec["temperature"] == 0.5          # temperature forwarded
    assert "seed" not in rec                  # seed omitted from the call


def test_generate_openai(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_openai",
                        lambda: _fake_openai(rec, content="hi", prompt_tokens=20,
                                             completion_tokens=5))
    r = llm.generate("openai:gpt-5.4-nano", "p", temperature=0.5, seed=42,
                     supports_temperature=False, supports_seed=True, max_tokens=256)
    assert (r.text, r.input_tokens, r.output_tokens) == ("hi", 20, 5)
    assert r.seed_applied == 42
    assert rec["seed"] == 42                   # seed forwarded
    assert "temperature" not in rec            # temperature omitted (flag False)
    # OpenAI's cap param name (max_completion_tokens, never max_tokens), carrying the
    # caller-supplied per-model value.
    assert rec["max_completion_tokens"] == 256 and "max_tokens" not in rec


def test_generate_xai(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_xai",
                        lambda: _fake_openai(rec, content="hi", prompt_tokens=20,
                                             completion_tokens=5))
    r = llm.generate("xai:grok-4.3", "p", temperature=0.5, seed=42,
                     supports_temperature=True, supports_seed=True, max_tokens=256)
    assert (r.text, r.input_tokens, r.output_tokens) == ("hi", 20, 5)
    assert r.seed_applied == 42
    assert rec["seed"] == 42                   # seed honored by x.ai
    assert rec["temperature"] == 0.5           # temperature forwarded
    assert rec["max_tokens"] == 256            # standard chat-completions cap, value carried


def test_generate_google(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_google",
                        lambda: _fake_google(rec, text="hi", prompt_token_count=20,
                                             candidates_token_count=5))
    r = llm.generate("google:gemini-2.5-flash-lite", "p", temperature=0.5, seed=42,
                     supports_temperature=True, supports_seed=True)
    assert (r.text, r.input_tokens, r.output_tokens) == ("hi", 20, 5)
    assert r.seed_applied == 42
    cfg = rec["config"]
    assert cfg.temperature == 0.5 and cfg.seed == 42   # both in the config object


# --- max_tokens threading (v9): the per-model cap reaches the right param ----
# A non-None value lands under each provider's correct cap parameter; None means
# uncapped and the adapter OMITS its cap param entirely (the temperature/seed
# omit-when-not-set pattern). The param NAME differs per provider and stays locked.

def test_anthropic_forwards_max_tokens_value(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_anthropic", lambda: _fake_anthropic(rec))
    llm.generate("anthropic:claude-haiku-4-5", "p", temperature=0.5, seed=None,
                 supports_temperature=True, supports_seed=False, max_tokens=321)
    assert rec["max_tokens"] == 321


def test_openai_forwards_max_tokens_value(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_openai", lambda: _fake_openai(rec))
    llm.generate("openai:gpt-5.4-nano", "p", temperature=0.5, seed=None,
                 supports_temperature=False, supports_seed=True, max_tokens=321)
    assert rec["max_completion_tokens"] == 321 and "max_tokens" not in rec


def test_xai_forwards_max_tokens_value(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_xai", lambda: _fake_openai(rec))
    llm.generate("xai:grok-4.3", "p", temperature=0.5, seed=None,
                 supports_temperature=True, supports_seed=True, max_tokens=321)
    assert rec["max_tokens"] == 321


def test_google_forwards_max_tokens_value(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_google", lambda: _fake_google(rec))
    llm.generate("google:gemini-2.5-flash-lite", "p", temperature=0.5, seed=None,
                 supports_temperature=True, supports_seed=True, max_tokens=321)
    assert rec["config"].max_output_tokens == 321


def test_anthropic_none_max_tokens_omits_cap(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_anthropic", lambda: _fake_anthropic(rec))
    llm.generate("anthropic:claude-haiku-4-5", "p", temperature=0.5, seed=None,
                 supports_temperature=True, supports_seed=False, max_tokens=None)
    assert "max_tokens" not in rec


def test_openai_none_max_tokens_omits_cap(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_openai", lambda: _fake_openai(rec))
    llm.generate("openai:gpt-5.4-nano", "p", temperature=0.5, seed=None,
                 supports_temperature=False, supports_seed=True, max_tokens=None)
    assert "max_completion_tokens" not in rec


def test_xai_none_max_tokens_omits_cap(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_xai", lambda: _fake_openai(rec))
    llm.generate("xai:grok-4.3", "p", temperature=0.5, seed=None,
                 supports_temperature=True, supports_seed=True, max_tokens=None)
    assert "max_tokens" not in rec


def test_google_none_max_tokens_omits_cap(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_google", lambda: _fake_google(rec))
    llm.generate("google:gemini-2.5-flash-lite", "p", temperature=0.5, seed=None,
                 supports_temperature=True, supports_seed=True, max_tokens=None)
    assert getattr(rec["config"], "max_output_tokens", None) is None


def test_generate_default_max_tokens_is_none_uncapped(monkeypatch):
    # The seam defaults max_tokens to None so the row-less seed_models.verify_xai_live
    # call keeps working and emits no cap. Omitting the arg == uncapped.
    rec = {}
    monkeypatch.setattr(llm, "_client_xai", lambda: _fake_openai(rec))
    llm.generate("xai:grok-4.3", "p", temperature=0.5, seed=None,
                 supports_temperature=True, supports_seed=True)
    assert "max_tokens" not in rec


# --- temperature range -----------------------------------------------------

def test_anthropic_temperature_out_of_range(monkeypatch):
    monkeypatch.setattr(llm, "_client_anthropic", lambda: _fake_anthropic({}))
    with pytest.raises(llm.LLMError, match="anthropic"):
        llm.generate("anthropic:claude-haiku-4-5", "p", temperature=1.5, seed=None,
                     supports_temperature=True, supports_seed=False)


def test_supports_temperature_false_omits_temperature(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_openai", lambda: _fake_openai(rec))
    # supports_temperature=False (the gpt-5 reasoning case): 1.5 would be out of any
    # nominal range, but temperature is omitted (not forwarded, not validated) so it
    # is accepted — the flag, not the model id, drives the omission now.
    llm.generate("openai:gpt-5.4-nano", "p", temperature=1.5, seed=None,
                 supports_temperature=False, supports_seed=True)
    assert "temperature" not in rec


def test_supports_temperature_true_forwards_temperature(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_openai", lambda: _fake_openai(rec))
    # A temperature-honoring OpenAI model (gpt-4o class) DOES accept temperature, so
    # the adapter forwards it (and seed) — the control must keep working for it.
    llm.generate("openai:gpt-4o", "p", temperature=0.5, seed=42,
                 supports_temperature=True, supports_seed=True)
    assert rec["temperature"] == 0.5
    assert rec["seed"] == 42


def test_openai_temperature_out_of_range_when_supported(monkeypatch):
    monkeypatch.setattr(llm, "_client_openai", lambda: _fake_openai({}))
    with pytest.raises(llm.LLMError, match="openai"):
        llm.generate("openai:gpt-4o", "p", temperature=2.5, seed=None,
                     supports_temperature=True, supports_seed=True)


def test_xai_temperature_out_of_range(monkeypatch):
    monkeypatch.setattr(llm, "_client_xai", lambda: _fake_openai({}))
    with pytest.raises(llm.LLMError, match="xai"):
        llm.generate("xai:grok-4.3", "p", temperature=2.5, seed=None,
                     supports_temperature=True, supports_seed=True)


def test_supports_seed_false_omits_seed(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_openai", lambda: _fake_openai(rec))
    # A model whose row says no-seed never forwards one, even on a seed-honoring
    # provider and a positive seed.
    r = llm.generate("openai:gpt-4o", "p", temperature=0.5, seed=42,
                     supports_temperature=True, supports_seed=False)
    assert "seed" not in rec
    assert r.seed_applied is None


def test_gemini_accepts_1_5_temperature(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_google", lambda: _fake_google(rec))
    llm.generate("google:gemini-2.5-flash-lite", "p", temperature=1.5, seed=None,
                 supports_temperature=True, supports_seed=True)
    assert rec["config"].temperature == 1.5


# --- missing key (real factory path, not patched) --------------------------

def test_missing_key_raises_named_error(monkeypatch):
    # Delete from os.environ; the factory's key check must fire BEFORE the SDK
    # import, surfacing a catchable LLMError (never an ImportError).
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(llm.LLMError, match="OPENAI_API_KEY"):
        llm.generate("openai:gpt-5.4-nano", "p", temperature=1.0, seed=None,
                     supports_temperature=False, supports_seed=True)


# --- live smoke (excluded by default; hits real provider APIs) --------------
# The four canonical price-map model strings, so live + [prices] stay in lockstep.

LIVE_MODELS = [
    "anthropic:claude-haiku-4-5",
    "openai:gpt-5.4-nano",
    "xai:grok-4.3",
    "google:gemini-2.5-flash-lite",
]


_SEED_FLAGS = {m["model"]: m for m in config.SEED_MODELS}


@pytest.mark.live
@pytest.mark.parametrize("model", LIVE_MODELS)
def test_live_generate(model):
    # A real call per provider, with the model's seeded capability flags. HARD-FAILS
    # (never skips) if the key is missing or the provider errors — the point of the
    # live gate.
    flags = _SEED_FLAGS[model]
    result = llm.generate(model, "Reply with the single word OK",
                          temperature=1.0, seed=7,
                          supports_temperature=bool(flags["supports_temperature"]),
                          supports_seed=bool(flags["supports_seed"]))
    assert result.text and result.text.strip(), f"{model}: empty text"
    assert result.input_tokens > 0, f"{model}: no input tokens reported"


# --- non-positive seed is dropped (xAI rejects seed <= 0) -------------------

def test_generate_drops_non_positive_seed(monkeypatch):
    # seed=0 (and negatives) are normalized to "no seed" in generate(), so the
    # adapter never forwards an invalid value and seed_applied is None.
    for bad in (0, -1):
        rec = {}
        monkeypatch.setattr(llm, "_client_xai", lambda: _fake_openai(rec))
        r = llm.generate("xai:grok-4.3", "p", temperature=0.5, seed=bad,
                         supports_temperature=True, supports_seed=True)
        assert "seed" not in rec, f"seed={bad} must not be forwarded"
        assert r.seed_applied is None


def test_generate_keeps_positive_seed(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_xai", lambda: _fake_openai(rec))
    r = llm.generate("xai:grok-4.3", "p", temperature=0.5, seed=7,
                     supports_temperature=True, supports_seed=True)
    assert rec["seed"] == 7 and r.seed_applied == 7


# --- provider API errors become clean LLMError (never a raw 500) ------------

def test_api_error_base_maps_each_provider():
    import openai
    import anthropic
    from google.genai import errors as genai_errors
    assert llm._api_error_base("openai") is openai.OpenAIError
    assert llm._api_error_base("xai") is openai.OpenAIError  # xAI rides openai SDK
    assert llm._api_error_base("anthropic") is anthropic.AnthropicError
    assert llm._api_error_base("google") is genai_errors.APIError


def _raising_openai(exc):
    class _Completions:
        def create(self, **kwargs):
            raise exc

    return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))


def test_generate_wraps_xai_api_error(monkeypatch):
    import openai
    monkeypatch.setattr(
        llm, "_client_xai",
        lambda: _raising_openai(openai.OpenAIError("boom from x.ai")))
    with pytest.raises(llm.LLMError) as exc:
        llm.generate("xai:grok-4.3", "p", temperature=0.5, seed=None,
                     supports_temperature=True, supports_seed=True)
    assert "xai" in str(exc.value) and "boom from x.ai" in str(exc.value)


def test_generate_wraps_openai_api_error(monkeypatch):
    import openai
    monkeypatch.setattr(
        llm, "_client_openai",
        lambda: _raising_openai(openai.OpenAIError("boom")))
    with pytest.raises(llm.LLMError) as exc:
        llm.generate("openai:gpt-5.4-nano", "p", temperature=0.5, seed=None,
                     supports_temperature=False, supports_seed=True)
    assert "openai" in str(exc.value)


# --- pin the seeded capability flags to the adapters (kills drift) ---------
# For each baseline model in config.SEED_MODELS, run generate() with that model's
# flags against the provider's fake and assert the adapter ACTUALLY did what the
# flags say: temperature reached the SDK call iff supports_temperature, and a seed
# governed the run iff supports_seed. temperature is a top-level kwarg for
# anthropic/openai/xai but rides inside the `config` object for google, so the
# forwarding check is per-provider — and it checks ABSENCE (key/attr missing),
# never "== None", so it cannot pass vacuously. This is the guard that the flags
# captured as data in SEED_MODELS still match the real adapter behavior.

_FACTORY_AND_FAKE = {
    "anthropic": ("_client_anthropic", _fake_anthropic),
    "openai": ("_client_openai", _fake_openai),
    "xai": ("_client_xai", _fake_openai),
    "google": ("_client_google", _fake_google),
    "ollama": ("_ollama_request", _fake_ollama),
}


def _temperature_forwarded(provider, rec):
    if provider == "google":
        cfg = rec.get("config")
        return cfg is not None and getattr(cfg, "temperature", None) is not None
    if provider == "ollama":
        return "temperature" in rec.get("options", {})
    return "temperature" in rec


@pytest.mark.parametrize("entry", config.SEED_MODELS,
                         ids=[m["model"] for m in config.SEED_MODELS])
def test_seed_flags_match_adapter(entry, monkeypatch):
    model = entry["model"]
    st, ss = bool(entry["supports_temperature"]), bool(entry["supports_seed"])
    provider = llm.split_model(model)[0]
    factory_name, fake = _FACTORY_AND_FAKE[provider]
    rec = {}
    if provider == "ollama":
        # The Ollama seam takes the request payload (not a zero-arg client factory).
        monkeypatch.setattr(llm, factory_name, lambda payload: fake(rec, payload))
    else:
        monkeypatch.setattr(llm, factory_name, lambda: fake(rec))

    # temperature 0.5 is valid for every provider's range (anthropic 0-1, others
    # 0-2); a positive seed is supplied so seed_applied reflects whether it governed.
    result = llm.generate(model, "p", temperature=0.5, seed=42,
                          supports_temperature=st, supports_seed=ss)

    assert (result.seed_applied is not None) == ss, (
        f"{model}: seed_applied={result.seed_applied!r} vs supports_seed={ss}"
    )
    assert _temperature_forwarded(provider, rec) == st, (
        f"{model}: temperature forwarded={_temperature_forwarded(provider, rec)} "
        f"vs supports_temperature={st}"
    )


# --- ollama adapter + the cross-provider think seam -------------------------
# The Ollama adapter is the one that honors `think`. These pin the seam contract:
# the bare tag reaches the request, think is gated BOTH directions, the off case
# persists 0 (not NULL), a non-honoring provider / non-reasoning model drops think,
# and any transport/parse failure becomes a clean LLMError (Fix A parity), never a
# raw exception. The network is mocked at llm._ollama_request (no socket).

def test_split_model_ollama_double_colon():
    # The regression canary for the whole feature: the canonical double-colon tag
    # must split on the FIRST colon so the model id keeps its own ':'.
    assert llm.split_model("ollama:qwen3.5:9b") == ("ollama", "qwen3.5:9b")


def test_ollama_in_supported_providers():
    assert "ollama" in llm.SUPPORTED_PROVIDERS
    assert "ollama" in llm.THINK_PROVIDERS


def _patch_ollama(monkeypatch, rec, **fake_kwargs):
    monkeypatch.setattr(
        llm, "_ollama_request",
        lambda payload: _fake_ollama(rec, payload, **fake_kwargs),
    )


def test_ollama_bare_tag_reaches_request(monkeypatch):
    rec = {}
    _patch_ollama(monkeypatch, rec)
    llm.generate("ollama:qwen3.5:9b", "p", temperature=0.5, seed=7,
                 supports_temperature=True, supports_seed=True)
    # split_model stripped the 'ollama:' prefix; the bare tag is sent.
    assert rec["model"] == "qwen3.5:9b"


def test_ollama_think_on_captures_thinking_and_applies_one(monkeypatch):
    rec = {}
    _patch_ollama(monkeypatch, rec, thinking="step by step")
    result = llm.generate("ollama:qwen3.5:9b", "p", temperature=0.5, seed=7,
                          supports_temperature=True, supports_seed=True,
                          think=True, is_reasoning=True)
    assert rec["think"] is True                 # top-level toggle sent on
    assert result.thinking == "step by step"    # reasoning content captured
    assert result.think_applied == 1


def test_ollama_think_off_persists_zero_not_null(monkeypatch):
    # LOAD-BEARING: an honored model toggled OFF must record think_applied == 0
    # (applied, chose off), NOT None. A False must survive generate()'s normalization.
    rec = {}
    _patch_ollama(monkeypatch, rec)             # fake returns no `thinking` field
    result = llm.generate("ollama:qwen3.5:9b", "p", temperature=0.5, seed=7,
                          supports_temperature=True, supports_seed=True,
                          think=False, is_reasoning=True)
    assert rec["think"] is False                # the toggle is still sent (off)
    assert result.thinking is None
    assert result.think_applied == 0            # 0, never None


def test_ollama_non_reasoning_model_drops_think(monkeypatch):
    # is_reasoning False -> think is normalized to None in generate(); the adapter
    # never receives it (no `think` key in the payload) and think_applied is None.
    rec = {}
    _patch_ollama(monkeypatch, rec)
    result = llm.generate("ollama:qwen3.5:9b", "p", temperature=0.5, seed=7,
                          supports_temperature=True, supports_seed=True,
                          think=True, is_reasoning=False)
    assert "think" not in rec
    assert result.think_applied is None


def test_think_dropped_for_non_honoring_provider(monkeypatch):
    # A reasoning-capable model on a provider NOT in THINK_PROVIDERS (openai): think
    # is dropped, the cloud adapter leaves thinking/think_applied None.
    rec = {}
    monkeypatch.setattr(llm, "_client_openai", lambda: _fake_openai(rec))
    result = llm.generate("openai:gpt-5.4-nano", "p", temperature=0.5, seed=7,
                          supports_temperature=False, supports_seed=True,
                          think=True, is_reasoning=True)
    assert result.thinking is None
    assert result.think_applied is None


def test_cloud_adapter_leaves_think_fields_none(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_anthropic", lambda: _fake_anthropic(rec))
    result = llm.generate("anthropic:claude-haiku-4-5", "p", temperature=0.5,
                          seed=None, supports_temperature=True, supports_seed=False)
    assert result.thinking is None and result.think_applied is None


def test_ollama_eval_count_is_output_tokens(monkeypatch):
    # eval_count (which already includes thinking tokens) maps to output_tokens;
    # prompt_eval_count maps to input_tokens.
    rec = {}
    _patch_ollama(monkeypatch, rec, prompt_eval_count=21, eval_count=476)
    result = llm.generate("ollama:qwen3.5:9b", "p", temperature=0.5, seed=7,
                          supports_temperature=True, supports_seed=True)
    assert result.input_tokens == 21 and result.output_tokens == 476


def test_ollama_connection_error_becomes_llmerror(monkeypatch):
    def boom(payload):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(llm, "_ollama_request", boom)
    with pytest.raises(llm.LLMError, match=r"^ollama: "):
        llm.generate("ollama:qwen3.5:9b", "p", temperature=0.5, seed=7,
                     supports_temperature=True, supports_seed=True)


def test_ollama_parses_a_real_captured_response(monkeypatch):
    # Evidence test: feed a REAL captured /api/generate body (testing/fixtures/
    # ollama_api_generate_response.json, think:true, stream:false) through the adapter
    # and prove the evidenced key names map correctly. If Ollama renames a key, this
    # is the regression that catches it.
    import json as _json
    from pathlib import Path
    body = _json.loads(
        (Path(__file__).parent / "fixtures"
         / "ollama_api_generate_response.json").read_text()
    )
    monkeypatch.setattr(llm, "_ollama_request", lambda payload: body)
    result = llm.generate("ollama:qwen3.5:9b", "p", temperature=0.1, seed=1,
                          supports_temperature=True, supports_seed=True,
                          think=True, is_reasoning=True)
    assert result.text == body["response"]
    assert result.thinking == body["thinking"]
    assert result.input_tokens == body["prompt_eval_count"]
    assert result.output_tokens == body["eval_count"]
    assert result.think_applied == 1


def test_ollama_malformed_response_becomes_llmerror(monkeypatch):
    # A body missing eval_count (model-not-running shape, or an API rename) must be a
    # clean LLMError, never a KeyError 500.
    monkeypatch.setattr(llm, "_ollama_request",
                        lambda payload: {"response": "hi", "prompt_eval_count": 5})
    with pytest.raises(llm.LLMError, match="ollama: malformed response"):
        llm.generate("ollama:qwen3.5:9b", "p", temperature=0.5, seed=7,
                     supports_temperature=True, supports_seed=True)
