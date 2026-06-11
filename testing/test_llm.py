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
        llm.generate("nope:model", "hi", temperature=0.5, seed=None)
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


# --- adapter happy paths + parameter forwarding ----------------------------

def test_generate_anthropic(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_anthropic",
                        lambda: _fake_anthropic(rec, text="hi", input_tokens=20,
                                                output_tokens=5))
    r = llm.generate("anthropic:claude-haiku-4-5", "p", temperature=0.5, seed=42)
    assert (r.text, r.input_tokens, r.output_tokens) == ("hi", 20, 5)
    assert r.seed_applied is None             # anthropic never applies a seed
    assert rec["temperature"] == 0.5          # temperature forwarded
    assert "seed" not in rec                  # seed omitted from the call


def test_generate_openai(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_openai",
                        lambda: _fake_openai(rec, content="hi", prompt_tokens=20,
                                             completion_tokens=5))
    r = llm.generate("openai:gpt-5.4-nano", "p", temperature=0.5, seed=42)
    assert (r.text, r.input_tokens, r.output_tokens) == ("hi", 20, 5)
    assert r.seed_applied == 42
    assert rec["seed"] == 42                   # seed forwarded
    assert "temperature" not in rec            # temperature omitted for openai
    assert "max_completion_tokens" in rec and "max_tokens" not in rec


def test_generate_xai(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_xai",
                        lambda: _fake_openai(rec, content="hi", prompt_tokens=20,
                                             completion_tokens=5))
    r = llm.generate("xai:grok-4-fast", "p", temperature=0.5, seed=42)
    assert (r.text, r.input_tokens, r.output_tokens) == ("hi", 20, 5)
    assert r.seed_applied == 42
    assert rec["seed"] == 42                   # seed honored by x.ai
    assert rec["temperature"] == 0.5           # temperature forwarded
    assert "max_tokens" in rec                 # standard chat-completions cap


def test_generate_google(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_google",
                        lambda: _fake_google(rec, text="hi", prompt_token_count=20,
                                             candidates_token_count=5))
    r = llm.generate("google:gemini-2.5-flash-lite", "p", temperature=0.5, seed=42)
    assert (r.text, r.input_tokens, r.output_tokens) == ("hi", 20, 5)
    assert r.seed_applied == 42
    cfg = rec["config"]
    assert cfg.temperature == 0.5 and cfg.seed == 42   # both in the config object


# --- temperature range -----------------------------------------------------

def test_anthropic_temperature_out_of_range(monkeypatch):
    monkeypatch.setattr(llm, "_client_anthropic", lambda: _fake_anthropic({}))
    with pytest.raises(llm.LLMError, match="anthropic"):
        llm.generate("anthropic:claude-haiku-4-5", "p", temperature=1.5, seed=None)


def test_openai_reasoning_omits_temperature(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_openai", lambda: _fake_openai(rec))
    # gpt-5.4-nano is a reasoning model: 1.5 would be out of any nominal range,
    # but temperature is omitted (not forwarded, not validated) so it is accepted.
    llm.generate("openai:gpt-5.4-nano", "p", temperature=1.5, seed=None)
    assert "temperature" not in rec


def test_is_openai_reasoning_model():
    assert llm._is_openai_reasoning_model("gpt-5.4-nano") is True
    assert llm._is_openai_reasoning_model("o3-mini") is True
    assert llm._is_openai_reasoning_model("gpt-4o") is False


def test_openai_non_reasoning_forwards_temperature(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_openai", lambda: _fake_openai(rec))
    # A non-reasoning OpenAI model (gpt-4o class) DOES accept temperature, so the
    # adapter forwards it (and seed) — the control must keep working for it.
    llm.generate("openai:gpt-4o", "p", temperature=0.5, seed=42)
    assert rec["temperature"] == 0.5
    assert rec["seed"] == 42


def test_openai_non_reasoning_temperature_out_of_range(monkeypatch):
    monkeypatch.setattr(llm, "_client_openai", lambda: _fake_openai({}))
    with pytest.raises(llm.LLMError, match="openai"):
        llm.generate("openai:gpt-4o", "p", temperature=2.5, seed=None)


def test_xai_temperature_out_of_range(monkeypatch):
    monkeypatch.setattr(llm, "_client_xai", lambda: _fake_openai({}))
    with pytest.raises(llm.LLMError, match="xai"):
        llm.generate("xai:grok-4-fast", "p", temperature=2.5, seed=None)


def test_gemini_accepts_1_5_temperature(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_google", lambda: _fake_google(rec))
    llm.generate("google:gemini-2.5-flash-lite", "p", temperature=1.5, seed=None)
    assert rec["config"].temperature == 1.5


# --- missing key (real factory path, not patched) --------------------------

def test_missing_key_raises_named_error(monkeypatch):
    # Delete from os.environ; the factory's key check must fire BEFORE the SDK
    # import, surfacing a catchable LLMError (never an ImportError).
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(llm.LLMError, match="OPENAI_API_KEY"):
        llm.generate("openai:gpt-5.4-nano", "p", temperature=1.0, seed=None)


# --- live smoke (excluded by default; hits real provider APIs) --------------
# The four canonical price-map model strings, so live + [prices] stay in lockstep.

LIVE_MODELS = [
    "anthropic:claude-haiku-4-5",
    "openai:gpt-5.4-nano",
    "xai:grok-4-fast",
    "google:gemini-2.5-flash-lite",
]


@pytest.mark.live
@pytest.mark.parametrize("model", LIVE_MODELS)
def test_live_generate(model):
    # A real call per provider. HARD-FAILS (never skips) if the key is missing or
    # the provider errors — that is the point of the live gate.
    result = llm.generate(model, "Reply with the single word OK",
                          temperature=1.0, seed=7)
    assert result.text and result.text.strip(), f"{model}: empty text"
    assert result.input_tokens > 0, f"{model}: no input tokens reported"


# --- non-positive seed is dropped (xAI rejects seed <= 0) -------------------

def test_generate_drops_non_positive_seed(monkeypatch):
    # seed=0 (and negatives) are normalized to "no seed" in generate(), so the
    # adapter never forwards an invalid value and seed_applied is None.
    for bad in (0, -1):
        rec = {}
        monkeypatch.setattr(llm, "_client_xai", lambda: _fake_openai(rec))
        r = llm.generate("xai:grok-4-fast", "p", temperature=0.5, seed=bad)
        assert "seed" not in rec, f"seed={bad} must not be forwarded"
        assert r.seed_applied is None


def test_generate_keeps_positive_seed(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "_client_xai", lambda: _fake_openai(rec))
    r = llm.generate("xai:grok-4-fast", "p", temperature=0.5, seed=7)
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
        llm.generate("xai:grok-4-fast", "p", temperature=0.5, seed=None)
    assert "xai" in str(exc.value) and "boom from x.ai" in str(exc.value)


def test_generate_wraps_openai_api_error(monkeypatch):
    import openai
    monkeypatch.setattr(
        llm, "_client_openai",
        lambda: _raising_openai(openai.OpenAIError("boom")))
    with pytest.raises(llm.LLMError) as exc:
        llm.generate("openai:gpt-5.4-nano", "p", temperature=0.5, seed=None)
    assert "openai" in str(exc.value)


# --- model_capabilities ----------------------------------------------------

def test_model_capabilities_anthropic_omits_seed():
    # Anthropic honors temperature but has no seed param.
    assert llm.model_capabilities("anthropic:claude-haiku-4-5") == {
        "temperature": True, "seed": False,
    }


def test_model_capabilities_openai_reasoning_omits_temperature():
    # gpt-5.4-nano is a reasoning model: temperature omitted, seed honored.
    assert llm.model_capabilities("openai:gpt-5.4-nano") == {
        "temperature": False, "seed": True,
    }


def test_model_capabilities_openai_non_reasoning_honors_both():
    assert llm.model_capabilities("openai:gpt-4o") == {
        "temperature": True, "seed": True,
    }


def test_model_capabilities_xai_and_google_honor_both():
    assert llm.model_capabilities("xai:grok-4-fast") == {
        "temperature": True, "seed": True,
    }
    assert llm.model_capabilities("google:gemini-2.5-flash-lite") == {
        "temperature": True, "seed": True,
    }


def test_model_capabilities_unknown_provider_raises():
    with pytest.raises(llm.LLMError) as exc:
        llm.model_capabilities("nope:model")
    for provider in ("anthropic", "openai", "xai", "google"):
        assert provider in str(exc.value)


# --- pin model_capabilities to the adapters (kills drift) ------------------
# For each shipped price-map model, run generate() against the provider's fake and
# assert capability AGREES with what the adapter actually did: seed honored iff
# seed_applied is not None, and temperature honored iff it reached the SDK call.
# temperature is a top-level kwarg for anthropic/openai/xai but rides inside the
# `config` object for google, so the forwarding check is per-provider — and it
# checks ABSENCE (key/attr missing), never "== None", so it cannot pass vacuously.

_FACTORY_AND_FAKE = {
    "anthropic": ("_client_anthropic", _fake_anthropic),
    "openai": ("_client_openai", _fake_openai),
    "xai": ("_client_xai", _fake_openai),
    "google": ("_client_google", _fake_google),
}


def _temperature_forwarded(provider, rec):
    if provider == "google":
        cfg = rec.get("config")
        return cfg is not None and getattr(cfg, "temperature", None) is not None
    return "temperature" in rec


@pytest.mark.parametrize("model", sorted(config.PRICES))
def test_capabilities_match_adapter(model, monkeypatch):
    provider = llm.split_model(model)[0]
    factory_name, fake = _FACTORY_AND_FAKE[provider]
    rec = {}
    monkeypatch.setattr(llm, factory_name, lambda: fake(rec))

    caps = llm.model_capabilities(model)
    # temperature 0.5 is valid for every provider's range (anthropic 0-1, others
    # 0-2); a seed is supplied so seed_applied reflects whether it was honored.
    result = llm.generate(model, "p", temperature=0.5, seed=42)

    assert (result.seed_applied is not None) == caps["seed"], (
        f"{model}: seed_applied={result.seed_applied!r} vs caps.seed={caps['seed']}"
    )
    assert _temperature_forwarded(provider, rec) == caps["temperature"], (
        f"{model}: temperature forwarded={_temperature_forwarded(provider, rec)} "
        f"vs caps.temperature={caps['temperature']}"
    )
