"""Third-party validator feeds for the price-refresh cross-check.

The scrape is the value-of-record; these feeds are an INDEPENDENT second opinion (not a
scrape mirror). LiteLLM is tried first; OpenRouter is the fetch-failure fallback. All
network is injectable (get_json) so the parse / fallback / lookup logic tests offline
against captured fixtures. Per-token feed prices normalize through validate.to_per_1m so
everything lands on the DB's per-1M unit before any compare.

The getter is a SEPARATE path from extract.fetch_page (which strips HTML and would
destroy the JSON), so it must carry the same browser User-Agent from config, or the raw
endpoints can 403 the way the provider pages did.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

from . import validate

DEFAULT_FETCH_TIMEOUT = 30.0

# Validator names (also the model_keys sub-key for each feed).
LITELLM = "litellm"
OPENROUTER = "openrouter"

# validator_lookup reasons. OK carries prices; the other two are no-price states kept
# DISTINCT: NO_VALIDATOR_ENTRY is a genuine validator gap (it does not carry the model),
# NO_KEY_MAPPING is OUR config gap (no model_keys entry) — loud and separate so a mapping
# drift never hides as a benign validator gap.
OK = "ok"
NO_KEY_MAPPING = "no_key_mapping"
NO_VALIDATOR_ENTRY = "no_validator_entry"


class ValidatorError(Exception):
    """A validator feed could not be fetched or parsed. Raised wrapped (never a raw
    httpx / JSON error) so the fallback and the Sources-this-run panel get a clean,
    human reason string."""


@dataclass
class FetchOutcome:
    """One URL actually attempted, for the Sources-this-run panel."""

    name: str                       # LITELLM / OPENROUTER
    url: str
    display_url: str
    ok: bool
    status: int | None = None
    error: str | None = None
    fetched_at: str | None = None


@dataclass
class ValidatorFeed:
    """The answering validator (or none) plus every attempt's outcome."""

    name: str | None                # which validator answered, or None if all failed
    by_key: dict = field(default_factory=dict)   # raw id -> {"input", "output"} per-1M
    fetched_at: str | None = None
    outcomes: list = field(default_factory=list)  # FetchOutcome per URL attempted


def fetch_json(url: str, *, user_agent: str, timeout: float = DEFAULT_FETCH_TIMEOUT,
               http_get=None):
    """Raw-JSON GET that FORWARDS the browser UA (the highest-risk line: the UA lives in
    extract.fetch_page, which this cannot reuse, so it is re-sent here). Returns
    (status, parsed_json). Raises ValidatorError on a network failure, a non-200 status,
    or unparseable JSON — never a raw httpx / JSON exception."""
    get = http_get or httpx.get
    try:
        resp = get(url, timeout=timeout, follow_redirects=True,
                   headers={"User-Agent": user_agent})
    except httpx.HTTPError as e:
        raise ValidatorError(f"fetch failed: {e}") from e
    status = getattr(resp, "status_code", None)
    if status != 200:
        raise ValidatorError(f"non-200 status {status}")
    try:
        return status, resp.json()
    except (ValueError, json.JSONDecodeError) as e:
        raise ValidatorError(f"unparseable JSON: {e}") from e


def _parse_litellm(data: object) -> dict:
    """LiteLLM: a top-level dict keyed by model id -> {input_cost_per_token,
    output_cost_per_token} (per-token floats). A junk/partial entry is skipped, not
    fatal. Returns id -> {"input", "output"} per-1M."""
    if not isinstance(data, dict):
        raise ValidatorError("litellm payload is not a dict")
    out: dict = {}
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        inp = entry.get("input_cost_per_token")
        outp = entry.get("output_cost_per_token")
        if inp is None or outp is None:
            continue
        try:
            out[key] = {
                "input": validate.to_per_1m(inp, validate.UNIT_PER_TOKEN),
                "output": validate.to_per_1m(outp, validate.UNIT_PER_TOKEN),
            }
        except ValueError:
            continue
    return out


def _parse_openrouter(data: object) -> dict:
    """OpenRouter: {"data": [{"id", "pricing": {"prompt", "completion"}}]} (per-token
    STRINGS). A junk/partial entry is skipped. Returns id -> {"input", "output"}
    per-1M."""
    if not isinstance(data, dict):
        raise ValidatorError("openrouter payload is not a dict")
    models = data.get("data")
    if not isinstance(models, list):
        raise ValidatorError("openrouter payload missing data[] list")
    out: dict = {}
    for entry in models:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("id")
        pricing = entry.get("pricing")
        if not mid or not isinstance(pricing, dict):
            continue
        prompt = pricing.get("prompt")
        completion = pricing.get("completion")
        if prompt is None or completion is None:
            continue
        try:
            out[mid] = {
                "input": validate.to_per_1m(prompt, validate.UNIT_PER_TOKEN),
                "output": validate.to_per_1m(completion, validate.UNIT_PER_TOKEN),
            }
        except ValueError:
            continue
    return out


def fetch_validator(*, litellm_url: str, openrouter_url: str,
                    litellm_display_url: str, openrouter_display_url: str,
                    user_agent: str, now: str, get_json=None) -> ValidatorFeed:
    """Try LiteLLM first; fall back to OpenRouter ONLY on a LiteLLM fetch failure (never
    both in the normal path). Records a FetchOutcome per URL ACTUALLY attempted, so a
    fallback run shows BOTH the failed primary and the answering fallback. Returns a
    ValidatorFeed with name=None and an empty by_key when both feeds fail."""
    get = get_json or fetch_json
    outcomes: list = []

    try:
        status, data = get(litellm_url, user_agent=user_agent)
        by_key = _parse_litellm(data)
        outcomes.append(FetchOutcome(LITELLM, litellm_url, litellm_display_url,
                                     ok=True, status=status, fetched_at=now))
        return ValidatorFeed(LITELLM, by_key, now, outcomes)
    except ValidatorError as e:
        outcomes.append(FetchOutcome(LITELLM, litellm_url, litellm_display_url,
                                     ok=False, error=str(e), fetched_at=now))

    try:
        status, data = get(openrouter_url, user_agent=user_agent)
        by_key = _parse_openrouter(data)
        outcomes.append(FetchOutcome(OPENROUTER, openrouter_url,
                                     openrouter_display_url, ok=True, status=status,
                                     fetched_at=now))
        return ValidatorFeed(OPENROUTER, by_key, now, outcomes)
    except ValidatorError as e:
        outcomes.append(FetchOutcome(OPENROUTER, openrouter_url,
                                     openrouter_display_url, ok=False, error=str(e),
                                     fetched_at=now))

    return ValidatorFeed(None, {}, None, outcomes)


def validator_lookup(feed: ValidatorFeed, model: str, model_keys: dict):
    """Resolve `model`'s validator prices via model_keys[model][feed.name], keying on the
    EXACT mapped id (no transform heuristic). Returns (prices_or_None, reason):
    NO_KEY_MAPPING when the model has no map entry for the answering validator (our gap),
    NO_VALIDATOR_ENTRY when the mapped id is absent from the feed (or no validator
    answered), OK with {"input", "output"} otherwise."""
    if feed.name is None:
        return None, NO_VALIDATOR_ENTRY
    entry = model_keys.get(model)
    if not entry or not entry.get(feed.name):
        return None, NO_KEY_MAPPING
    prices = feed.by_key.get(entry[feed.name])
    if prices is None:
        return None, NO_VALIDATOR_ENTRY
    return prices, OK


def check_map_completeness(active_models, model_keys: dict,
                           validator_name: str | None) -> list:
    """The silent-failure guard: every active, non-local registry model MUST carry a
    model_keys entry for the answering validator, or it quietly stops being cross-checked
    while the page shows a benign state. Returns the sorted models missing a mapping for
    `validator_name` (surfaced as the loud 'no key mapping' state, distinct from a genuine
    validator gap). Returns [] when no validator answered (nothing to map against)."""
    if validator_name is None:
        return []
    missing = [m for m in active_models
               if not (model_keys.get(m) or {}).get(validator_name)]
    return sorted(missing)
