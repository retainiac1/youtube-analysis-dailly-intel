"""Validator-feed fetch + parse + lookup + completeness (no live network).

Drives `price_refresh.validators` with an injected getter over the committed LiteLLM /
OpenRouter fixtures, so the fallback, normalization, and lookup-reason logic test offline.
"""
import json
from pathlib import Path

import pytest

from price_refresh import validators

FIXTURES = Path(__file__).resolve().parent.parent / "price_refresh" / "fixtures"
LITELLM = json.loads((FIXTURES / "litellm_sample.json").read_text())
OPENROUTER = json.loads((FIXTURES / "openrouter_sample.json").read_text())

MODEL_KEYS = {
    "anthropic:claude-haiku-4-5":
        {"litellm": "claude-haiku-4-5", "openrouter": "anthropic/claude-haiku-4.5"},
    "openai:gpt-5.4-nano":
        {"litellm": "gpt-5.4-nano", "openrouter": "openai/gpt-5.4-nano"},
    "xai:grok-4.3":
        {"litellm": "xai/grok-4.3", "openrouter": "x-ai/grok-4.3"},
    "google:gemini-2.5-flash-lite":
        {"litellm": "gemini-2.5-flash-lite",
         "openrouter": "google/gemini-2.5-flash-lite"},
}

URLS = dict(
    litellm_url="https://litellm.example/feed.json",
    openrouter_url="https://openrouter.example/api/v1/models",
    litellm_display_url="https://github.example/litellm",
    openrouter_display_url="https://openrouter.example/models",
)
NOW = "2026-06-14T12:00:00-04:00"


def _getter(mapping):
    """Build an injected getter: url -> (status, data) for ok, or a ValidatorError to
    simulate a fetch failure for that url."""
    def get_json(url, *, user_agent):
        result = mapping.get(url)
        if isinstance(result, Exception):
            raise result
        return result
    return get_json


# --- fetch_validator: LiteLLM-first, OpenRouter-fallback --------------------

def test_litellm_answers_first_no_fallback():
    feed = validators.fetch_validator(
        **URLS, user_agent="ua", now=NOW,
        get_json=_getter({URLS["litellm_url"]: (200, LITELLM)}))
    assert feed.name == validators.LITELLM
    # grok normalizes per-token 1.25e-6/2.5e-6 -> per-1M 1.25/2.50.
    assert feed.by_key["xai/grok-4.3"]["input"] == pytest.approx(1.25)
    assert feed.by_key["xai/grok-4.3"]["output"] == pytest.approx(2.50)
    # Only LiteLLM was attempted (no fallback when the primary answers).
    assert [o.name for o in feed.outcomes] == [validators.LITELLM]
    assert feed.outcomes[0].ok


def test_openrouter_fallback_fires_only_on_litellm_failure_and_records_both():
    feed = validators.fetch_validator(
        **URLS, user_agent="ua", now=NOW,
        get_json=_getter({
            URLS["litellm_url"]: validators.ValidatorError("non-200 status 403"),
            URLS["openrouter_url"]: (200, OPENROUTER),
        }))
    assert feed.name == validators.OPENROUTER
    assert feed.by_key["x-ai/grok-4.3"]["input"] == pytest.approx(1.25)
    # BOTH rows recorded: the failed primary AND the answering fallback. Never hide
    # the failed primary validator.
    names = [o.name for o in feed.outcomes]
    assert names == [validators.LITELLM, validators.OPENROUTER]
    assert feed.outcomes[0].ok is False and "403" in feed.outcomes[0].error
    assert feed.outcomes[1].ok is True


def test_both_feeds_fail_yields_no_answering_validator():
    feed = validators.fetch_validator(
        **URLS, user_agent="ua", now=NOW,
        get_json=_getter({
            URLS["litellm_url"]: validators.ValidatorError("timeout"),
            URLS["openrouter_url"]: validators.ValidatorError("non-200 status 500"),
        }))
    assert feed.name is None
    assert feed.by_key == {}
    assert [o.ok for o in feed.outcomes] == [False, False]


# --- validator_lookup: OK / NO_KEY_MAPPING / NO_VALIDATOR_ENTRY -------------

def test_validator_lookup_ok_returns_normalized_prices():
    feed = validators.fetch_validator(
        **URLS, user_agent="ua", now=NOW,
        get_json=_getter({URLS["litellm_url"]: (200, LITELLM)}))
    prices, reason = validators.validator_lookup(
        feed, "xai:grok-4.3", MODEL_KEYS)
    assert reason == validators.OK
    assert prices["input"] == pytest.approx(1.25)


def test_validator_lookup_no_key_mapping_when_model_absent_from_map():
    # Our config gap: the model has no model_keys entry for the answering validator.
    feed = validators.fetch_validator(
        **URLS, user_agent="ua", now=NOW,
        get_json=_getter({URLS["litellm_url"]: (200, LITELLM)}))
    prices, reason = validators.validator_lookup(
        feed, "openai:gpt-9-unmapped", MODEL_KEYS)
    assert prices is None
    assert reason == validators.NO_KEY_MAPPING


def test_validator_lookup_no_validator_entry_when_feed_lacks_mapped_id():
    # Genuine validator gap: mapping exists, but the feed does not carry that id. This
    # is DISTINCT from NO_KEY_MAPPING.
    feed = validators.fetch_validator(
        **URLS, user_agent="ua", now=NOW,
        get_json=_getter({URLS["litellm_url"]: (200, LITELLM)}))
    keys = {**MODEL_KEYS,
            "xai:grok-4.3": {"litellm": "xai/not-in-feed", "openrouter": "x-ai/grok-4.3"}}
    prices, reason = validators.validator_lookup(feed, "xai:grok-4.3", keys)
    assert prices is None
    assert reason == validators.NO_VALIDATOR_ENTRY


# --- check_map_completeness (the silent-failure guard) ----------------------

def test_completeness_flags_a_model_missing_its_mapping():
    missing = validators.check_map_completeness(
        {"xai:grok-4.3", "openai:gpt-9-unmapped"}, MODEL_KEYS, validators.LITELLM)
    assert missing == ["openai:gpt-9-unmapped"]


def test_completeness_clean_when_all_mapped():
    missing = validators.check_map_completeness(
        set(MODEL_KEYS), MODEL_KEYS, validators.LITELLM)
    assert missing == []


# --- fetch_json default getter forwards the browser UA ----------------------

def test_fetch_json_forwards_user_agent_header():
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"ok": True}

    def http_get(url, *, timeout, follow_redirects, headers):
        seen.update(headers)
        return _Resp()

    status, data = validators.fetch_json(
        "https://x.example/feed.json", user_agent="MyBrowserUA/1.0",
        http_get=http_get)
    assert status == 200 and data == {"ok": True}
    # The UA MUST be forwarded, or raw.githubusercontent / the feeds can 403.
    assert seen.get("User-Agent") == "MyBrowserUA/1.0"


def test_fetch_json_non_200_raises_validator_error():
    class _Resp:
        status_code = 403

        def json(self):
            return {}

    status_err = validators.fetch_json
    with pytest.raises(validators.ValidatorError, match="403"):
        status_err("https://x.example/feed.json", user_agent="ua",
                   http_get=lambda url, **kw: _Resp())
