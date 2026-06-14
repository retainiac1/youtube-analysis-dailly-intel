"""Pure validation layers for the price-refresh agent.

No I/O, no DB, no network — every function is a deterministic transform over plain
data, independently testable, and composed in gate order so each layer gates the
next. Thresholds are ALWAYS passed in (no hardcoded defaults that hide the source);
config.PRICE_REFRESH is the single home for the real values.

A "prices payload" is a mapping of canonical ``provider:model`` -> a dict carrying at
least ``input`` and ``output`` per-MTok rates (extra keys like ``quote`` /
``source_url`` are ignored here). The canonical model set passed to check_structure
must be the EXTRACTABLE/PRICED subset (local/$0 models excluded) — never the full
registry, or extraction would fail "missing key" on a model with no pricing page.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# The three terminal outcomes, in contract precedence.
REJECT = "REJECT"
AUTO = "AUTO"
REVIEW = "REVIEW"

# check_cross_source outcomes.
CROSS_AGREE = "agree"
CROSS_PASSTHROUGH = "passthrough"
CROSS_CONFLICT = "conflict"

# Price-quote units, for to_per_1m. Provider pages quote per-1M-tokens already; the
# validator feeds quote per-token (LiteLLM as a float, OpenRouter as a string).
UNIT_PER_1M = "per_1m"
UNIT_PER_TOKEN = "per_token"
_TOKENS_PER_1M = 1_000_000

# Cross-check cell flags (scrape vs validator, per field). FIVE deliberately-distinct
# states: the last two are NOT mismatch. mismatch is a real price CONFLICT; unverified
# is "the validator does not carry this model" (no signal); no_key_mapping is "we have
# no model_keys entry for it" (our config gap) — kept loud and separate so a mapping
# drift can never hide as a benign unverified. match/drift/mismatch come from
# classify_cell; unverified/no_key_mapping are set by the caller from the lookup reason.
CELL_MATCH = "match"
CELL_DRIFT = "drift"
CELL_MISMATCH = "mismatch"
CELL_UNVERIFIED = "unverified"
CELL_NO_KEY_MAPPING = "no_key_mapping"

_FIELDS = ("input", "output")


@dataclass
class CheckResult:
    """A pass/fail gate result. On failure, ``reason`` names the offending key."""

    ok: bool
    reason: str = ""


@dataclass
class DeltaResult:
    """The delta-band classification of one value vs its baseline."""

    outcome: str          # AUTO or REVIEW
    pct: float            # signed fractional change (new - last) / last
    direction: str        # "up" / "down" / "none"


@dataclass
class CrossResult:
    """The cross-source verdict for one value across two independent sources."""

    outcome: str               # CROSS_AGREE / CROSS_PASSTHROUGH / CROSS_CONFLICT
    value: float | None        # the value to carry forward; None on conflict
    moved_source: str | None = None   # "a" / "b" on passthrough, else None


@dataclass
class ClassifyResult:
    """The terminal classification of one price: REJECT / AUTO / REVIEW."""

    outcome: str
    reason: str = ""
    pct: float | None = None
    direction: str | None = None


def _is_number(value: object) -> bool:
    """True for a real int/float, rejecting bool (an int subclass) and everything
    else — the same external-typo guard the config validator uses."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def to_per_1m(value: object, unit: str) -> float:
    """Normalize ONE price quote to per-1M-tokens (the DB's input/output_per_1m unit).

    The single conversion path so no scattered ``* 1e6`` can drift: a provider page is
    already per-1M (returned unchanged); a validator per-token quote (LiteLLM float or
    OpenRouter string) scales up by 1e6. Accepts a numeric string for the per-token case
    (OpenRouter quotes strings) but rejects bool and non-numeric junk loudly so a bad
    feed value cannot silently become a price. Raises ValueError on an unknown unit."""
    if isinstance(value, bool):
        raise ValueError(f"price value must be numeric, not bool: {value!r}")
    try:
        num = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"price value is not numeric: {value!r}") from e
    if unit == UNIT_PER_1M:
        return num
    if unit == UNIT_PER_TOKEN:
        return num * _TOKENS_PER_1M
    raise ValueError(f"unknown price unit {unit!r}")


def classify_cell(scraped: float, validator: float, *, tolerance_pct: float,
                  review_band_pct: float) -> str:
    """Cross-check ONE field (input or output) of the scrape against the validator,
    both already normalized to per-1M. Returns CELL_MATCH (within tolerance_pct),
    CELL_DRIFT (beyond tolerance but within review_band_pct), or CELL_MISMATCH (beyond
    the review band — a real conflict). Per FIELD: the caller scores input and output
    separately so one clean field never masks a conflicting one. UNVERIFIED /
    NO_KEY_MAPPING are NOT decided here — they are no-validator-value states the caller
    sets from the lookup reason, kept distinct from this real-conflict mismatch."""
    if math.isclose(scraped, validator, rel_tol=tolerance_pct):
        return CELL_MATCH
    if math.isclose(scraped, validator, rel_tol=review_band_pct):
        return CELL_DRIFT
    return CELL_MISMATCH


def check_structure(payload: object, canonical_models: set[str]) -> CheckResult:
    """Gate 1 (structural). The payload must be a mapping whose keys EQUAL the
    canonical (extractable) model set exactly — no extra, no missing — and every
    model's ``input`` and ``output`` must be a positive number. Returns ok, or a
    failure naming the first offending key. Fail -> the whole payload is REJECT.
    """
    if not isinstance(payload, dict):
        return CheckResult(False, f"payload must be a dict, got {type(payload).__name__}")
    keys = set(payload.keys())
    missing = canonical_models - keys
    if missing:
        return CheckResult(False, f"missing model key(s): {sorted(missing)}")
    extra = keys - canonical_models
    if extra:
        return CheckResult(False, f"unexpected model key(s): {sorted(extra)}")
    for model, entry in payload.items():
        if not isinstance(entry, dict):
            return CheckResult(False, f"{model}: value must be a dict")
        for field in _FIELDS:
            value = entry.get(field)
            if not _is_number(value):
                return CheckResult(False, f"{model}.{field} must be a number, got {value!r}")
            if value <= 0:
                return CheckResult(False, f"{model}.{field} must be positive, got {value}")
    return CheckResult(True)


def check_magnitude(prices: dict, lo: float, hi: float) -> CheckResult:
    """Gate 2 (magnitude/unit). Every ``input``/``output`` per-MTok rate must fall
    within [lo, hi]. Catches the 1000x unit error and per-token-vs-per-MTok confusion.
    Runs BEFORE the delta band so nonsense is rejected outright, not mistaken for a
    large legitimate change. Fail -> REJECT, naming the offending key."""
    for model, entry in prices.items():
        for field in _FIELDS:
            value = entry[field]
            if not (lo <= value <= hi):
                return CheckResult(
                    False,
                    f"{model}.{field} = {value} out of band [{lo}, {hi}]",
                )
    return CheckResult(True)


def check_output_gt_input(prices: dict) -> list[str]:
    """SOFT layer. Output normally exceeds input (true for all current budget
    models). Return the list of model keys where ``output <= input`` — a violation
    FLAGS for review, it does not reject, so a genuine future inversion is surfaced
    to a human rather than silently dropped."""
    return [model for model, entry in prices.items()
            if entry["output"] <= entry["input"]]


def check_cross_source(a: float, b: float, last_kept: float,
                       tol: float) -> CrossResult:
    """Compare two independent sources for ONE value, with THREE outcomes:

    - agree within ``tol`` -> CROSS_AGREE (carry the agreed value); the delta band
      then decides AUTO vs REVIEW.
    - disagree, but exactly ONE source matches ``last_kept`` within ``tol`` while the
      other moved -> CROSS_PASSTHROUGH: a lagging source on a real-move day. Carry the
      MOVED value through (recording which source moved), NOT a reject.
    - disagree, and NEITHER anchors to ``last_kept`` -> CROSS_CONFLICT (reject):
      independent sources rarely share a wrong number and neither matches known-good.
    """
    if math.isclose(a, b, rel_tol=tol):
        return CrossResult(CROSS_AGREE, a)
    a_anchored = math.isclose(a, last_kept, rel_tol=tol)
    b_anchored = math.isclose(b, last_kept, rel_tol=tol)
    if b_anchored and not a_anchored:
        return CrossResult(CROSS_PASSTHROUGH, a, moved_source="a")
    if a_anchored and not b_anchored:
        return CrossResult(CROSS_PASSTHROUGH, b, moved_source="b")
    return CrossResult(CROSS_CONFLICT, None)


def classify_delta(new: float, last: float, threshold: float) -> DeltaResult:
    """Per-value delta band: AUTO when the move is under ``threshold``, REVIEW at or
    above. Carries the signed fractional change and direction. ``last`` is the current
    active price (> 0 for any extractable model); a zero baseline routes any nonzero
    new value to REVIEW (an unbounded move a human should see)."""
    direction = "up" if new > last else "down" if new < last else "none"
    if last == 0:
        pct = 0.0 if new == 0 else math.inf
    else:
        pct = (new - last) / last
    outcome = AUTO if abs(pct) < threshold else REVIEW
    return DeltaResult(outcome, pct, direction)


def classify_price(new: float, last: float, *, lo: float, hi: float,
                   threshold: float, flagged: bool = False) -> ClassifyResult:
    """Compose the gates into the terminal outcome for ONE price, in contract
    precedence:

    1. magnitude/unit FIRST — out of [lo, hi] -> REJECT (a unit error wins over the
       delta band, even when the move is also >= threshold).
    2. delta band — >= threshold -> REVIEW, else AUTO.
    3. a soft ``flagged`` (an output<=input inversion or a lagging-source passthrough)
       upgrades an otherwise-AUTO move to REVIEW; it never rescues a REJECT.
    """
    if not (lo <= new <= hi):
        return ClassifyResult(
            REJECT, f"magnitude {new} out of band [{lo}, {hi}]")
    delta = classify_delta(new, last, threshold)
    if delta.outcome == REVIEW:
        return ClassifyResult(REVIEW, "delta >= threshold", delta.pct, delta.direction)
    if flagged:
        return ClassifyResult(REVIEW, "soft-flagged for review", delta.pct,
                              delta.direction)
    return ClassifyResult(AUTO, "sane and small", delta.pct, delta.direction)
