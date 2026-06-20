"""Tests for classify.py — the English-only + no-kids LLM gate core.

The core is DB/llm-free: classify_video drives an INJECTED generate_fn, so these
tests never touch the network. A fake generate_fn returns a GenerateResult-shaped
object (or raises) to exercise every branch.
"""
from types import SimpleNamespace

import pytest

import classify


def fake_video(title="Build better habits", description="A daily routine.",
               channel="HabitsChannel", tags=("habits", "routine"),
               audio="en", default_lang="en"):
    """A videos.list-shaped item with the snippet fields the prompt reads."""
    snippet = {"title": title, "description": description, "channelTitle": channel}
    if tags is not None:
        snippet["tags"] = list(tags)
    if audio is not None:
        snippet["defaultAudioLanguage"] = audio
    if default_lang is not None:
        snippet["defaultLanguage"] = default_lang
    return {"id": "v1", "snippet": snippet}


def gen_returning(payload_text, *, input_tokens=120, output_tokens=20):
    """A fake generate_fn that returns a GenerateResult-shaped object."""
    def _gen(prompt):
        return SimpleNamespace(text=payload_text, input_tokens=input_tokens,
                               output_tokens=output_tokens)
    return _gen


def json_text(english=True, kids_targeted=False, kids_subject=False, reason="ok"):
    import json
    return json.dumps({"english": english, "kids_targeted": kids_targeted,
                       "kids_subject": kids_subject, "reason": reason})


# --- prompt -----------------------------------------------------------------

def test_prompt_includes_metadata_fields():
    v = fake_video(title="Morning routine", channel="WellnessTV",
                   description="How I start my day", tags=("morning", "wellness"),
                   audio="en")
    prompt = classify.build_classification_prompt(v)
    assert "Morning routine" in prompt
    assert "WellnessTV" in prompt
    assert "How I start my day" in prompt
    assert "morning" in prompt
    assert "en" in prompt
    # JSON-only contract is stated.
    assert "JSON" in prompt


def test_prompt_handles_missing_tags_gracefully():
    v = fake_video(tags=None)
    prompt = classify.build_classification_prompt(v)
    # No crash, and the absence is rendered as a marker, not as evidence.
    assert "(none)" in prompt


# --- parse ------------------------------------------------------------------

def test_parse_valid_json():
    parsed = classify.parse_classification(json_text(english=True, kids_subject=True))
    assert parsed == {"english": True, "kids_targeted": False,
                      "kids_subject": True, "reason": "ok"}


def test_parse_strips_code_fence():
    fenced = "```json\n" + json_text() + "\n```"
    parsed = classify.parse_classification(fenced)
    assert parsed["english"] is True


def test_parse_unparseable_raises():
    with pytest.raises(classify.ClassificationError, match="unparseable"):
        classify.parse_classification("not json at all")


def test_parse_missing_key_raises():
    import json
    text = json.dumps({"english": True, "kids_targeted": False, "reason": "x"})
    with pytest.raises(classify.ClassificationError, match="kids_subject"):
        classify.parse_classification(text)


def test_parse_non_bool_raises():
    import json
    text = json.dumps({"english": "yes", "kids_targeted": False,
                       "kids_subject": False, "reason": "x"})
    with pytest.raises(classify.ClassificationError, match="english"):
        classify.parse_classification(text)


def test_parse_non_object_raises():
    with pytest.raises(classify.ClassificationError):
        classify.parse_classification("[1, 2, 3]")


# --- classify_video ---------------------------------------------------------

def test_classify_keeps_english_non_kids():
    v = classify.classify_video(fake_video(), generate_fn=gen_returning(json_text()))
    assert v.keep is True
    assert v.english is True
    assert v.input_tokens == 120 and v.output_tokens == 20


def test_classify_drops_non_english():
    g = gen_returning(json_text(english=False))
    v = classify.classify_video(fake_video(), generate_fn=g)
    assert v.keep is False
    assert v.english is False


def test_classify_drops_kids_targeted():
    g = gen_returning(json_text(kids_targeted=True))
    v = classify.classify_video(fake_video(), generate_fn=g)
    assert v.keep is False
    assert v.kids_targeted is True


def test_classify_drops_kids_subject():
    g = gen_returning(json_text(kids_subject=True))
    v = classify.classify_video(fake_video(), generate_fn=g)
    assert v.keep is False
    assert v.kids_subject is True


def test_classify_propagates_parse_error():
    g = gen_returning("garbage")
    with pytest.raises(classify.ClassificationError):
        classify.classify_video(fake_video(), generate_fn=g)


def test_classify_does_not_swallow_generate_error():
    class Boom(Exception):
        pass

    def g(prompt):
        raise Boom("provider down")

    with pytest.raises(Boom):
        classify.classify_video(fake_video(), generate_fn=g)


# --- resolvers (monkeypatched db, no schema needed) -------------------------

def test_active_model_falls_back_to_config(monkeypatch):
    monkeypatch.setattr(classify.db, "get_preference", lambda conn, key: None)
    import config
    assert classify.active_classification_model(None) == config.CLASSIFICATION_MODEL
    assert (classify.active_classification_fallback_model(None)
            == config.CLASSIFICATION_FALLBACK_MODEL)


def test_active_model_uses_persisted_preference(monkeypatch):
    prefs = {classify.CLASSIFICATION_MODEL_PREF: "xai:grok-4.3"}
    monkeypatch.setattr(classify.db, "get_preference",
                        lambda conn, key: prefs.get(key))
    assert classify.active_classification_model(None) == "xai:grok-4.3"
