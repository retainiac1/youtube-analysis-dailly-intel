"""LLM classification gate: English-only + no-kids over each discovered video.

A binary verdict the YouTube Data API cannot express (relevanceLanguage is a soft
bias; madeForKids misses content merely *about* children), so it runs as one LLM
call per survivor in the discover pipeline.

Mirrors price_refresh/extract.py: the core here (build_classification_prompt,
parse_classification, classify_video) is DB-free and llm-free, driven by an INJECTED
generate_fn(prompt) -> GenerateResult-shaped object (with .text/.input_tokens/
.output_tokens). swipefile binds the real llm.generate + db logging at the phase, so
the dev loop stays deterministic and offline.

Language detection is metadata-only (title/description/channel/tags + the declared
language fields). It cannot observe an audio-first Short's spoken language, so a video
with English-looking text but foreign audio and no language tag can still pass — a
known, accepted limitation; the per-video reason is logged so misses are auditable.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import config
import db

# Bump whenever the prompt text/contract below changes. Cached verdicts on a resumed
# run are stamped with this; a mismatch invalidates them so a tuned prompt re-runs
# (see swipefile._classify_survivors).
CLASSIFY_PROMPT_VERSION = 1

# app_preferences keys. A future manual-run UI persists an override here; the
# unattended cron falls back to the settings.toml defaults. The shared resolvers below
# keep cron + UI from diverging, mirroring price_refresh.daily.active_extraction_model.
CLASSIFICATION_MODEL_PREF = "classification_model"
CLASSIFICATION_FALLBACK_PREF = "classification_fallback_model"

# The three booleans the model must return. `reason` is free text (optional).
_BOOL_KEYS = ("english", "kids_targeted", "kids_subject")


class ClassificationError(Exception):
    """A parse/contract failure: unparseable JSON, a non-object payload, or a missing/
    non-boolean key. Surfaced cleanly (never a raw JSONDecodeError/KeyError) so the
    phase treats it as a per-video failure to retry / fail over, never a guess."""


class ClassificationUnavailable(Exception):
    """Raised when classification cannot run at all (both the primary and fallback
    providers failed preflight, or a mid-run mass failure). The phase lets it abort the
    run loudly rather than silently leak foreign/kids content or empty the board."""


@dataclass
class Verdict:
    """One video's classification outcome. `keep` is the gate decision."""

    english: bool
    kids_targeted: bool
    kids_subject: bool
    reason: str
    input_tokens: int
    output_tokens: int

    @property
    def keep(self) -> bool:
        """A survivor must be English and neither targeted-at nor about children."""
        return self.english and not self.kids_targeted and not self.kids_subject


def active_classification_model(conn) -> str:
    """The primary classifier in effect: the persisted app_preferences choice if set,
    else the settings.toml CLASSIFICATION_MODEL default. The cron and any future UI
    resolve through here so they never diverge."""
    return db.get_preference(conn, CLASSIFICATION_MODEL_PREF) or config.CLASSIFICATION_MODEL


def active_classification_fallback_model(conn) -> str:
    """The backup classifier in effect (different provider from the primary, enforced
    by validate_config): persisted choice if set, else CLASSIFICATION_FALLBACK_MODEL."""
    return (db.get_preference(conn, CLASSIFICATION_FALLBACK_PREF)
            or config.CLASSIFICATION_FALLBACK_MODEL)


def _strip_fences(text: str) -> str:
    """Return the JSON body, unwrapping a ```json ... ``` (or bare ```) fence if the
    model added one despite the JSON-only instruction. Falls back to the stripped text.
    """
    candidate = text.strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
    return match.group(1).strip() if match else candidate


def build_classification_prompt(video: dict) -> str:
    """Strict JSON-only prompt over the video's text metadata + declared language
    fields. Tags are rendered as '(none)' when absent so missing tags are never read as
    signal. The model returns {english, kids_targeted, kids_subject, reason}."""
    snippet = video.get("snippet", {})
    title = snippet.get("title", "")
    description = snippet.get("description", "")
    channel = snippet.get("channelTitle", "")
    tags = snippet.get("tags") or []
    audio_lang = snippet.get("defaultAudioLanguage", "")
    default_lang = snippet.get("defaultLanguage", "")
    return (
        "You are a precise content classifier for short videos. Read the metadata "
        "below and return JSON ONLY — no prose, no markdown, no code fences.\n\n"
        'Return exactly this object: {"english": <bool>, "kids_targeted": <bool>, '
        '"kids_subject": <bool>, "reason": <string>}.\n\n'
        "- english: true ONLY if the video's primary language is English. Judge from "
        "the title, description, channel name, tags, and the declared language fields. "
        "If a non-English language is declared, or the language is genuinely ambiguous "
        "from the available text, return false.\n"
        "- kids_targeted: true if the video is made FOR a child audience (nursery "
        "rhymes, kids' cartoons, toddler learning, content whose intended viewer is a "
        "young child).\n"
        "- kids_subject: true if the video is primarily ABOUT or prominently FEATURES "
        "children or parenting/child-rearing (parenting tips, family routines centered "
        "on kids, a child as the main subject), even when the intended viewer is an "
        "adult.\n"
        "- reason: one short sentence justifying the flags.\n\n"
        f"Title: {title}\n"
        f"Channel: {channel}\n"
        f"Declared audio language: {audio_lang or '(none)'}\n"
        f"Declared language: {default_lang or '(none)'}\n"
        f"Tags: {', '.join(tags) if tags else '(none)'}\n"
        f"Description:\n{description}\n"
    )


def parse_classification(text: str) -> dict:
    """Defensively parse the model's response into {english, kids_targeted,
    kids_subject, reason}. Strips a stray fence, json.loads (-> ClassificationError),
    requires a JSON object with all three booleans actually boolean. `reason` is
    coerced to a string (empty when absent/non-string). Any failure ->
    ClassificationError (never a raw JSONDecodeError/KeyError)."""
    try:
        payload = json.loads(_strip_fences(text))
    except (json.JSONDecodeError, ValueError) as e:
        raise ClassificationError(f"unparseable JSON from model: {e}") from e
    if not isinstance(payload, dict):
        raise ClassificationError(
            f"expected a JSON object, got {type(payload).__name__}")
    out: dict = {}
    for key in _BOOL_KEYS:
        val = payload.get(key)
        if not isinstance(val, bool):
            raise ClassificationError(
                f"key {key!r} must be a JSON boolean, got {val!r}")
        out[key] = val
    reason = payload.get("reason", "")
    out["reason"] = reason.strip() if isinstance(reason, str) else ""
    return out


def classify_video(video: dict, *, generate_fn) -> Verdict:
    """Classify one video. Builds the prompt, calls the injected
    `generate_fn(prompt) -> GenerateResult` (whose errors propagate — the phase owns
    retry/failover), and parses the response into a Verdict. A bad response raises
    ClassificationError."""
    prompt = build_classification_prompt(video)
    result = generate_fn(prompt)
    parsed = parse_classification(result.text)
    return Verdict(
        english=parsed["english"],
        kids_targeted=parsed["kids_targeted"],
        kids_subject=parsed["kids_subject"],
        reason=parsed["reason"],
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )
