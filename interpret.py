"""Interpretation generator core.

Assembles a lane's data for the selected window (aggregated dashboard metrics or the
raw underlying rows), builds a prompt, calls the single `llm.generate` seam, and
validates the model's response against the structured JSON contract (see
INTERP_SECTION_KEYS). Persists the result: one canonical row per (run_date, scope) in
`interpretations` (the stored `text` is the contract JSON, code-stamped with
`contract_version` and `context_mode`) plus one append-only row in `llm_invocations`
recording the full parameter set (including the context mode). Importable by the CLI
and the dashboard endpoint, so the button is a thin wrapper rather than a
reimplementation.

Invariants (see docs/interpretations-generator-plan.MD):
- Synthesize first (network), THEN one short transaction; never hold a write lock
  across network I/O.
- Pinned write order inside one transaction: log_invocation, then
  upsert_interpretation (so the atomicity test is meaningful).
- `model` is the canonical combined "provider:model" string everywhere; it is
  passed straight to generate() (which splits it) and stored verbatim.
- `llm_invocations.seed` records the seed that ACTUALLY governed the call
  (result.seed_applied), not what the user typed; `filter` is NULL in v1.
- run_date comes from `rankings`, never from `now`; now_local_iso() is only the
  generated_at timestamp.
"""

import argparse
import json
import sys
import time

import config
import db
from config import DB_PATH, ConfigError, VALID_BUCKETS, now_local_iso, validate_config
from llm import estimate_cost, generate, split_model, LLMError

# Lane order matches compute_rankings keys: "overall" plus VALID_BUCKETS. Derived
# from the single source of truth (config.VALID_BUCKETS) so adding a bucket does not
# require editing two places; sorted() makes the order deterministic.
SCOPES = ("overall",) + tuple(sorted(VALID_BUCKETS))

# A sane default valid for every provider's temperature range (Anthropic 0-1,
# others 0-2). The adapter validates against its own range downstream.
DEFAULT_TEMPERATURE = 1.0


# --- Structured JSON interpretation contract --------------------------------
# Everything hangs off these named constants: the prompt builders, the validator,
# and (later phases) the frontend all read them, so the contract is defined ONCE.

# Stamped onto every stored object so downstream readers branch on the version
# instead of content-sniffing the blob. Bump only when the stored shape changes.
INTERP_CONTRACT_VERSION = 1

# THE empty-section sentinel. The prompt instructs the model to emit this EXACT
# string for a section with no data, and the validator / all consumers detect
# emptiness by equality with it (never a fuzzy match). A free-form "no data"
# phrasing would defeat the grounding rule and any show/hide-empty logic.
INTERP_NO_DATA = "no data"

# The data context that produced an interpretation, recorded per run.
INTERP_CONTEXT_MODES = ("aggregated", "raw")

# The reason codes a refused raw-mode estimate can carry (see _raw_cost_estimate). These
# cross the server/UI boundary: the pre-run estimate endpoint returns one, and the frontend
# maps it to copy AND fail-closes on it, so the code is defined ONCE here and reused by the
# estimator, exposed via /api/interpret-defaults, and asserted against in tests. A silent
# string mismatch here would let a refused run render with Run enabled.
INTERP_REFUSE_OVER_CAP = "over_cap"    # priced estimate exceeds INTERP_RAW_COST_CAP_USD
INTERP_REFUSE_UNPRICED = "unpriced"    # paid model with no current price window (fail closed)
INTERP_REFUSE_REASONS = (INTERP_REFUSE_OVER_CAP, INTERP_REFUSE_UNPRICED)

# The 12 section keys, in canonical order, each bound to the Dashboard tab and the
# `fetch_dashboard_*` payload key(s) that feed it. SINGLE source: this drives the
# aggregated payload assembly and the ordered key tuple below. `breakouts` is one
# section feeding both the top-ratio chart and the leaderboard table; `survivorship`
# folds rank movement and lifespan distribution (they render in one group).
SECTION_SOURCES = {
    "subniche_volume": ("topic_mix", ("subniche_counts",)),
    "youtube_category": ("topic_mix", ("category_counts",)),
    "topic_tags": ("topic_mix", ("topic_counts",)),
    "ratio_bands": ("breakout", ("band_counts",)),
    "breakouts": ("breakout", ("leaderboard",)),
    "title_anatomy": ("format", ("title_stats",)),
    "duration": ("format", ("durations",)),
    "likes_vs_comments": ("format", ("like_comment_pairs",)),
    "publish_times": ("format", ("publish_heatmap",)),
    "growth": ("lifecycle", ("growth",)),
    "survivorship": ("lifecycle", ("rank_history", "lifespan_distribution")),
    "engagement": ("lifecycle", ("ratio_series", "maturation")),
}
INTERP_SECTION_KEYS = tuple(SECTION_SOURCES)

# tab -> the db producer, called once per tab in aggregated mode. The lifecycle
# output also carries `summary` (whole-window medians), fed as recommendation
# context rather than a 13th section.
_AGG_PRODUCERS = {
    "topic_mix": db.fetch_dashboard_topic_mix,
    "breakout": db.fetch_dashboard_breakout,
    "format": db.fetch_dashboard_format,
    "lifecycle": db.fetch_dashboard_lifecycle,
}

# Broad per-video columns dumped in raw mode (videos-level only, no channel join):
# the model sees the underlying rows and derives each section itself. This is the
# firehose the raw-mode spend cap (Gate 3) exists to bound.
_RAW_SELECT_COLS = (
    "v.video_id, v.title, v.channel_title, v.published_at, v.duration_seconds, "
    "v.category_id, v.topic_categories, v.matched_queries, v.view_count, "
    "v.like_count, v.comment_count, v.views_to_subs_ratio, v.views_per_day"
)


# --- Prompt fields (server-owned spec) --------------------------------------
# The SINGLE source of truth for which per-video signals the prompt may carry: it
# drives the dashboard's multi-select options, the request validation, and the
# rendering below — so they cannot drift. `rank` and `title` are always-on anchors
# (every line needs them) and are NOT in this list; everything here is selectable.
# Each renderer reads ONE fetch_lane column and returns a short string, or None to
# omit (so a NULL never becomes the literal "None").

def _r_channel(row):
    v = row["channel_title"]
    return f"by {v}" if v is not None else None


def _r_views(row):
    v = row["view_count"]
    return f"{v} views" if v is not None else None


def _r_ratio(row):
    v = row["views_to_subs_ratio"]
    return f"views/subs {v}" if v is not None else None


def _r_likes(row):
    v = row["like_count"]
    return f"{v} likes" if v is not None else None


def _r_comments(row):
    # comment_count is meaningful at 0 (a video with no comments), so show it
    # whenever the column is present; only a NULL column is omitted.
    v = row["comment_count"]
    return f"{v} comments" if v is not None else None


def _r_views_per_day(row):
    v = row["views_per_day"]
    return f"{int(v)}/day" if v is not None else None


def _r_duration(row):
    v = row["duration_seconds"]
    return f"{v}s" if v is not None else None


def _r_published(row):
    v = row["published_at"]
    # Show the date only (no datetime parsing, so an odd value can never raise).
    return f"published {v[:10]}" if v else None


def _r_matched(row):
    v = row["matched_queries"]
    if not v:
        return None
    qs = [q for q in v.split("|") if q]
    return f'matched "{", ".join(qs)}"' if qs else None


# Top comments are stored pipe-delimited as `@handle: text (likes)|...`. Render at
# most 3, each trimmed to ~100 chars, so the heaviest field stays bounded.
_TOP_COMMENTS_MAX = 3
_COMMENT_TRIM = 100


def _r_top_comments(row):
    v = row["top_comments"]
    if not v or v == "[]":
        return None
    items = [c.strip() for c in v.split("|") if c.strip()][:_TOP_COMMENTS_MAX]
    if not items:
        return None
    trimmed = [(c[:_COMMENT_TRIM] + "…") if len(c) > _COMMENT_TRIM else c
               for c in items]
    return "comments: " + " | ".join(trimmed)


# Ordered spec: the canonical field order, the dashboard labels, and the renderer.
PROMPT_FIELDS = [
    {"key": "channel_title", "label": "Channel", "render": _r_channel},
    {"key": "view_count", "label": "Views", "render": _r_views},
    {"key": "views_to_subs_ratio", "label": "Views/subs ratio", "render": _r_ratio},
    {"key": "like_count", "label": "Likes", "render": _r_likes},
    {"key": "comment_count", "label": "Comments", "render": _r_comments},
    {"key": "views_per_day", "label": "Views/day", "render": _r_views_per_day},
    {"key": "duration_seconds", "label": "Duration", "render": _r_duration},
    {"key": "published_at", "label": "Published date", "render": _r_published},
    {"key": "matched_queries", "label": "Matched query", "render": _r_matched},
    {"key": "top_comments", "label": "Top comments", "render": _r_top_comments},
]
_FIELD_ORDER = [f["key"] for f in PROMPT_FIELDS]
_RENDERERS = {f["key"]: f["render"] for f in PROMPT_FIELDS}
# Default selection when no preference is stored: the populated signals, minus the
# heavy/noisy top_comments (the user can turn it on).
DEFAULT_PROMPT_FIELDS = [k for k in _FIELD_ORDER if k != "top_comments"]

# The dashboard needs the options WITHOUT the (non-JSON) render callables.
PROMPT_FIELD_OPTIONS = [{"key": f["key"], "label": f["label"]} for f in PROMPT_FIELDS]


def normalize_fields(fields) -> list:
    """Reduce a requested field list to known keys in canonical order, deduped.
    None, empty, or an all-unknown list falls back to DEFAULT_PROMPT_FIELDS, so a
    stale or hand-sent value can never produce a contentless prompt. The ONE place
    the selection is sanitized; both the write (persist) and render paths use it."""
    if not fields:
        return list(DEFAULT_PROMPT_FIELDS)
    chosen = {f for f in fields if f in _RENDERERS}
    if not chosen:
        return list(DEFAULT_PROMPT_FIELDS)
    return [k for k in _FIELD_ORDER if k in chosen]


def build_prompt(run_date: str, scope: str, rows: list, fields=None) -> str:
    """Assemble the synthesis prompt from a lane's ranked rows, including only the
    selected per-video `fields` (normalized against PROMPT_FIELDS; None → the
    default set). States the scope and the EXACT row count so the model cannot
    overstate a trend from a thin set, and asks for 3-4 sentences. Single source of
    truth for prompt text.

    `rows` are fetch_lane rows (sqlite3.Row); fetch_lane LEFT JOINs videos, so any
    video field may be NULL when the video row is missing. Each renderer omits a
    NULL rather than emitting the literal "None"; `rank` and `title` anchor the
    line (title omitted when NULL)."""
    keys = normalize_fields(fields)
    renderers = [_RENDERERS[k] for k in keys]
    lines = []
    for row in rows:
        title = row["title"]
        anchor = f'#{row["rank"]} "{title}"' if title is not None else f"#{row['rank']}"
        segs = [s for s in (r(row) for r in renderers) if s]
        lines.append(anchor + "\n   " + " · ".join(segs) if segs else anchor)

    body = "\n".join(lines)
    return (
        f"You are analyzing the '{scope}' leaderboard for the run dated "
        f"{run_date}. It contains exactly {len(rows)} ranked short-form "
        f"video(s), listed below by rank. Each line shows the selected signals "
        f"(which may include engagement, view velocity, duration, recency, and "
        f"the search query that surfaced the video).\n\n"
        f"{body}\n\n"
        f"Write a 3-4 sentence summary of what stands out in this lane, weaving in "
        f"the signals shown where they matter. Base every claim only on these "
        f"{len(rows)} rows; do not infer a broader trend than {len(rows)} "
        f"video(s) can support."
    )


# --- Structured contract: instructions, data assembly, validation -----------

def _contract_instructions(scope: str, start_date: str, end_date: str) -> str:
    """The shared prompt preamble that pins the JSON contract. Interpolates the exact
    section keys and the sentinel from the constants (one source), so the prompt and
    the validator can never drift on either the key set or the empty marker."""
    keys = ", ".join(INTERP_SECTION_KEYS)
    # An unbounded (None) bound is an all-time window; describe it in words rather than
    # printing "None" into the prompt.
    start_label = start_date if start_date else "the earliest run"
    end_label = end_date if end_date else "the latest run"
    return (
        f"You are analyzing the '{scope}' lane over the window {start_label} to "
        f"{end_label}. Return ONLY a single JSON object (no prose, no code fence) with "
        f'this exact shape: {{"sections": {{<one entry per section key>}}, '
        f'"recommendation": {{"suggestion": "<one concrete idea for next week\'s '
        f'videos>", "based_on": [<section keys that justify it>]}}}}.\n'
        f"The section keys are EXACTLY these 12, all required: {keys}.\n"
        f"Each section value is a 1 to 3 sentence plain-text interpretation of that "
        f"section's data. For any section that has NO data in this lane and window, "
        f'set its value to the EXACT string "{INTERP_NO_DATA}" and nothing else, '
        f"rather than inventing a narrative.\n"
        f'"based_on" must list only section keys whose value is not "{INTERP_NO_DATA}" '
        f"(ground the suggestion in sections that actually have data). If EVERY section "
        f'is "{INTERP_NO_DATA}", return an empty "based_on" list and set "suggestion" '
        f'to "{INTERP_NO_DATA}".'
    )


def _aggregated_section_payloads(conn, bucket: str, start_date, end_date):
    """Call each of the four dashboard producers once, then slice their outputs into
    the per-section payloads keyed by INTERP_SECTION_KEYS. Returns (payloads, summary)
    where summary is the lifecycle whole-window medians (recommendation context)."""
    tab_outputs = {
        tab: fn(conn, bucket, start_date, end_date)
        for tab, fn in _AGG_PRODUCERS.items()
    }
    payloads = {}
    for skey, (tab, pkeys) in SECTION_SOURCES.items():
        out = tab_outputs[tab]
        payloads[skey] = {pk: out.get(pk) for pk in pkeys}
    summary = tab_outputs["lifecycle"].get("summary")
    return payloads, summary


def build_aggregated_prompt(scope, start_date, end_date, section_payloads,
                            summary) -> str:
    """Aggregated-mode prompt: the contract preamble plus each section's precomputed
    dashboard aggregate, so the model interprets fixed-size, cheap inputs."""
    lines = [_contract_instructions(scope, start_date, end_date), "",
             "Per-section computed aggregates (interpret each):"]
    for skey in INTERP_SECTION_KEYS:
        lines.append(f"[{skey}]")
        lines.append(json.dumps(section_payloads.get(skey), default=str,
                                sort_keys=True))
    lines.append("")
    lines.append("Whole-window medians (context for the recommendation, not a "
                 "section):")
    lines.append(json.dumps(summary, default=str, sort_keys=True))
    return "\n".join(lines)


def build_raw_prompt(scope, start_date, end_date, rows) -> str:
    """Raw-mode prompt: the contract preamble plus the underlying per-video rows, one
    JSON object per row. The model derives every section from the raw data. This is
    the token firehose the raw-mode cap bounds."""
    lines = [_contract_instructions(scope, start_date, end_date), "",
             f"Underlying rows: {len(rows)} distinct video(s) that ranked in this "
             f"lane over the window. Derive every section from these rows:"]
    for row in rows:
        lines.append(json.dumps({k: row[k] for k in row.keys()}, default=str,
                                sort_keys=True))
    return "\n".join(lines)


def _raw_cost_estimate(conn, model: str, prompt: str, row_count: int) -> dict:
    """Estimate the cost of ONE raw interpretation run and decide whether to refuse it.
    THE single source of the estimate: both the pre-run endpoint and the synthesize_lane
    breaker call this over the SAME build_raw_prompt payload, so they cannot disagree.

    `est_input_tokens` is the built prompt's char length over config.INTERP_CHARS_PER_TOKEN
    (deliberately low, so it over-counts dense JSON rather than under-counting). Priced at
    TODAY's rate (the spend is billed now, not on the historical run_date). Fail-closed
    policy: a LOCAL model (config.LOCAL_PROVIDERS) is free and always allowed; a PAID+priced
    model is refused when the estimate exceeds config.INTERP_RAW_COST_CAP_USD; a PAID model
    with no current price window is refused (the guard cannot bound spend it cannot price).
    Returns the breakdown plus `over_cap` (the dollar test) and `refused` (the actual
    don't-spend decision) and a `reason`. Tunables are read from the config module at call
    time so a monkeypatched cap / a settings.toml edit takes effect without re-import."""
    cpt = config.INTERP_CHARS_PER_TOKEN
    est_input_tokens = (len(prompt) + cpt - 1) // cpt          # int ceil-division
    est_output_tokens = config.INTERP_EST_OUTPUT_TOKENS
    cap_usd = config.INTERP_RAW_COST_CAP_USD
    provider, _ = split_model(model)
    est_cost_usd = None
    over_cap = False
    refused = False
    reason = None
    if provider in config.LOCAL_PROVIDERS:
        est_cost_usd = 0.0                                     # local inference is free
    else:
        price = db.fetch_effective_price(conn, model, now_local_iso()[:10])
        if price is None:
            refused = True                                     # paid + unpriced: fail closed
            reason = INTERP_REFUSE_UNPRICED
        else:
            prices = {model: {"input": price["input_per_1m"],
                              "output": price["output_per_1m"]}}
            est_cost_usd = estimate_cost(model, est_input_tokens, est_output_tokens,
                                         prices)
            over_cap = est_cost_usd is not None and est_cost_usd > cap_usd
            refused = over_cap
            reason = INTERP_REFUSE_OVER_CAP if over_cap else None
    return {"row_count": row_count, "est_input_tokens": est_input_tokens,
            "est_output_tokens": est_output_tokens, "est_cost_usd": est_cost_usd,
            "cap_usd": cap_usd, "over_cap": over_cap, "refused": refused,
            "reason": reason}


def estimate_raw_interpretation(conn, bucket: str, start_date: str, end_date: str,
                                model: str) -> dict:
    """Pre-run cost/row-count estimate for a raw interpretation, surfaced before running
    (the dashboard endpoint reads this). Builds the ACTUAL payload the model would receive
    (the same build_raw_prompt the breaker uses), then defers to _raw_cost_estimate, so the
    surfaced number and the enforced cap are computed identically."""
    rows = db._dashboard_video_rows(conn, bucket, start_date, end_date, _RAW_SELECT_COLS)
    prompt = build_raw_prompt(bucket, start_date, end_date, rows)
    return _raw_cost_estimate(conn, model, prompt, len(rows))


def _degraded_sections():
    """Every section filled with the sentinel: the fully-unparseable fallback."""
    return {k: INTERP_NO_DATA for k in INTERP_SECTION_KEYS}


def validate_interpretation(raw) -> dict:
    """Validate a model interpretation response against the contract on receipt.
    Define-once, never raises (graceful degrade): a malformed or partial response
    yields what parsed plus flags for what did not, never a 500. `raw` may be the raw
    JSON string or an already-parsed object.

    Returns {"sections": {all 12 keys -> str}, "recommendation": {"suggestion": str,
    "based_on": [str]}, "missing": [keys], "errors": [str], "ok": bool}. Every section
    key is always present in the result (missing/invalid ones filled with the sentinel
    and listed in `missing`), so downstream readers can always index all 12. The
    grounding rule is enforced with the sentinel: `based_on` keeps only keys whose
    section value is not the sentinel; an empty `based_on` is valid ONLY when every
    section is the sentinel (a fully-empty window)."""
    errors: list = []
    obj = raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            obj = None
            errors.append("response was not valid JSON")
    if not isinstance(obj, dict):
        if not errors:
            errors.append("response was not a JSON object")
        return {"sections": _degraded_sections(),
                "recommendation": {"suggestion": INTERP_NO_DATA, "based_on": []},
                "missing": list(INTERP_SECTION_KEYS), "errors": errors, "ok": False}

    raw_sections = obj.get("sections")
    if not isinstance(raw_sections, dict):
        raw_sections = {}
        errors.append("sections missing or not an object")
    sections = {}
    missing: list = []
    for k in INTERP_SECTION_KEYS:
        v = raw_sections.get(k)
        if isinstance(v, str):
            sections[k] = v
        else:
            sections[k] = INTERP_NO_DATA
            missing.append(k)

    raw_rec = obj.get("recommendation")
    if not isinstance(raw_rec, dict):
        raw_rec = {}
        errors.append("recommendation missing or not an object")
    suggestion = raw_rec.get("suggestion")
    if not isinstance(suggestion, str):
        suggestion = INTERP_NO_DATA
        errors.append("recommendation.suggestion missing or not a string")
    raw_based_on = raw_rec.get("based_on")
    if not isinstance(raw_based_on, list):
        raw_based_on = []
        errors.append("recommendation.based_on missing or not a list")
    # Keep only real, non-sentinel sections (single grounding rule). Preserve order,
    # drop duplicates.
    based_on: list = []
    for k in raw_based_on:
        if (k in INTERP_SECTION_KEYS and sections[k] != INTERP_NO_DATA
                and k not in based_on):
            based_on.append(k)
    any_data = any(v != INTERP_NO_DATA for v in sections.values())
    if not based_on and any_data:
        errors.append("based_on empty despite non-empty sections")

    return {"sections": sections,
            "recommendation": {"suggestion": suggestion, "based_on": based_on},
            "missing": missing, "errors": errors, "ok": not (errors or missing)}


def synthesize_lane(conn, run_date: str, scope: str, model: str, *,
                    temperature: float, seed, think=None, filter=None,
                    fields=None, context_mode="aggregated",
                    start_date=None, end_date=None) -> dict:
    """Synthesize one lane over its window into the structured JSON contract: assemble
    the mode's data, and if the lane has any videos call generate(), validate the
    response, then persist. An empty lane (no videos in the window) skips the LLM call
    and writes no rows, preserving the empty-lane behavior.

    `context_mode` (INTERP_CONTEXT_MODES) selects the data fed to the model: "aggregated"
    interprets the four precomputed dashboard aggregates; "raw" dumps the underlying
    per-video rows. `start_date`/`end_date` bound the window; both default to `run_date`
    (a single-run window), so storage keying stays (run_date, scope). `scope` is the
    lane, which is identical to the rankings bucket.

    `model` is the canonical "provider:model" string. Its capability flags
    (supports_temperature / supports_seed / is_reasoning / max_tokens) are read from the
    `models` table here (the one place holding a connection) and threaded into generate(),
    keeping llm.py DB-free. An unregistered model raises LLMError (the dashboard turns it
    into a clean 400). `think` is the per-run reasoning toggle; generate() forwards it only
    for a reasoning-capable model on a think-honoring provider, otherwise the applied value
    (result.think_applied) is None. `fields` is accepted for call-site compatibility and no
    longer shapes the prompt (the per-video field selection belonged to the old prose path).
    Network happens BEFORE the transaction opens. The two writes run in ONE transaction in
    the pinned order (log_invocation, then upsert_interpretation), wrapped in
    run_with_db_retry for the lock case. The stored `text` is the contract JSON, code-stamped
    with `contract_version` and `context_mode`; a partial/malformed model response degrades
    (what parsed is kept, the rest flagged) rather than blanking."""
    if context_mode not in INTERP_CONTEXT_MODES:
        raise LLMError(
            f"context_mode must be one of {list(INTERP_CONTEXT_MODES)}, "
            f"got {context_mode!r}"
        )
    bucket = scope
    # `start_date`/`end_date` flow through UNCOERCED: None means an unbounded bound (an
    # all-time window), which the producers and the membership subquery already handle.
    # We do NOT default them to run_date -- that would bound an all-time request to a
    # single run and key it run_date:run_date instead of all:all. Single-run callers
    # (synthesize_run, the CLI) pass start=end=run_date explicitly. window_key is the
    # storage key for this exact window (the one helper, shared with fetch + migration).
    window_key = db.interpretation_window_key(start_date, end_date)

    # Population = the lane's distinct videos over the window (bounded rankings
    # membership). An empty population means nothing to interpret: skip like an empty
    # lane, no generate() call, no rows written.
    population = db._dashboard_video_rows(
        conn, bucket, start_date, end_date, "v.video_id")
    if not population:
        return {"scope": scope, "skipped": True}

    model_row = db.fetch_model(conn, model)
    if model_row is None:
        raise LLMError(
            f"model {model!r} is not in the registry; add it on the /models page"
        )

    if context_mode == "raw":
        rows = db._dashboard_video_rows(
            conn, bucket, start_date, end_date, _RAW_SELECT_COLS)
        prompt = build_raw_prompt(scope, start_date, end_date, rows)
        # Raw is the token firehose: estimate and refuse BEFORE spending. Same
        # _raw_cost_estimate over the same prompt the /estimate endpoint uses, so the
        # pre-run surface and this guard cannot disagree. Aggregated mode is fixed-size
        # and never capped. `refused` covers over-cap AND paid-but-unpriced (fail closed).
        estimate = _raw_cost_estimate(conn, model, prompt, len(rows))
        if estimate["refused"]:
            return {"scope": scope, "context_mode": "raw", "refused": True,
                    "estimate": estimate, "skipped": False}
    else:
        payloads, summary = _aggregated_section_payloads(
            conn, bucket, start_date, end_date)
        prompt = build_aggregated_prompt(scope, start_date, end_date, payloads,
                                         summary)

    # Time the network call: this is the generator "run time" we persist and show.
    # monotonic() is immune to wall-clock adjustments. The DB write below is trivial
    # and deliberately excluded so the figure reflects the model, not SQLite.
    start = time.monotonic()
    result = generate(model, prompt, temperature=temperature, seed=seed,
                      supports_temperature=bool(model_row["supports_temperature"]),
                      supports_seed=bool(model_row["supports_seed"]),
                      think=think,
                      is_reasoning=bool(model_row["is_reasoning"]),
                      max_tokens=model_row["max_tokens"])
    duration_ms = int((time.monotonic() - start) * 1000)
    now = now_local_iso()

    # Validate on receipt and code-stamp the stored object. A partial response keeps
    # what parsed and records the flags rather than blanking.
    validated = validate_interpretation(result.text)
    stored = {
        "contract_version": INTERP_CONTRACT_VERSION,
        "context_mode": context_mode,
        "sections": validated["sections"],
        "recommendation": validated["recommendation"],
    }
    partial = bool(validated["errors"] or validated["missing"])
    if partial:
        stored["_partial"] = True
        stored["_errors"] = validated["errors"]
        stored["_missing"] = validated["missing"]
    text = json.dumps(stored)

    def _write():
        with db.transaction(conn):
            db.log_invocation(conn, run_date, scope, model, temperature,
                              result.seed_applied, filter, result.input_tokens,
                              result.output_tokens, now, duration_ms=duration_ms,
                              context_mode=context_mode)
            # Persist the APPLIED parameters: the requested temperature, the seed that
            # actually governed (result.seed_applied), and the applied think value
            # (result.think_applied: NULL when think did not apply, 0/1 when it did),
            # mirroring what log_invocation records. The stored text is the contract JSON.
            # Keyed by window_key; run_date is provenance; duration_ms is denormalized
            # onto the row so the read never joins llm_invocations.
            db.upsert_interpretation(conn, window_key, scope, text, model, now,
                                     run_date=run_date,
                                     start_date=start_date, end_date=end_date,
                                     temperature=temperature,
                                     seed=result.seed_applied,
                                     think=result.think_applied,
                                     duration_ms=duration_ms)

    db.run_with_db_retry(_write)

    return {
        "scope": scope,
        "skipped": False,
        "interpretation": stored,
        "text": text,
        "context_mode": context_mode,
        "partial": partial,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "seed_applied": result.seed_applied,
        "think_applied": result.think_applied,
        "thinking": result.thinking,
        "duration_ms": duration_ms,
    }


def synthesize_run(conn, run_date: str, model: str, *, temperature: float,
                   seed, scopes=SCOPES, fields=None,
                   context_mode="aggregated") -> list:
    """Synthesize every scope for a run sequentially with one model + parameter set
    and one context mode, returning the per-scope outcomes (written or skipped-empty).
    This is the single-run path (the CLI), so the window is explicitly start=end=run_date
    -- synthesize_lane no longer defaults None bounds to run_date."""
    return [
        synthesize_lane(conn, run_date, scope, model,
                        temperature=temperature, seed=seed, fields=fields,
                        context_mode=context_mode,
                        start_date=run_date, end_date=run_date)
        for scope in scopes
    ]


def _resolve_run_date(conn, requested):
    """The default: the most recent run_date present in `rankings`. The generator
    never derives run_date from `now`. Raises ValueError when `rankings` is empty;
    the caller (main / a dashboard endpoint) decides how to surface that — this
    importable core stays free of process-exit coupling."""
    if requested is not None:
        return requested
    run_dates = db.fetch_run_dates(conn)
    if not run_dates:
        raise ValueError("no run_date present in rankings; nothing to summarize")
    return run_dates[0]["run_date"]


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Generate LLM interpretations for ranked lanes.")
    parser.add_argument("--model", required=True,
                        help="canonical provider:model, e.g. "
                             "anthropic:claude-haiku-4-5")
    parser.add_argument("--run-date", default=None,
                        help="Eastern run_date to summarize "
                             "(default: latest in rankings)")
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE,
                        help=f"sampling temperature (default {DEFAULT_TEMPERATURE}; "
                             "validated per-provider downstream)")
    parser.add_argument("--seed", type=int, default=None,
                        help="optional integer seed (ignored by providers that "
                             "do not support it)")
    parser.add_argument("--scope", choices=SCOPES, default=None,
                        help="limit to one lane (default: all three)")
    parser.add_argument("--context-mode", choices=INTERP_CONTEXT_MODES,
                        default="aggregated",
                        help="data context fed to the model: aggregated (dashboard "
                             "metrics, cheap) or raw (underlying rows)")
    parser.add_argument("--fields", default=None,
                        help="comma-separated per-video prompt fields "
                             f"(default: {','.join(DEFAULT_PROMPT_FIELDS)}); "
                             f"choices: {','.join(_FIELD_ORDER)}")
    args = parser.parse_args(argv)
    fields = ([f.strip() for f in args.fields.split(",") if f.strip()]
              if args.fields else None)

    try:
        validate_config()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        raise SystemExit(1)

    db.init_db(DB_PATH)
    conn = db.get_connection(DB_PATH)
    try:
        try:
            run_date = _resolve_run_date(conn, args.run_date)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            raise SystemExit(1)
        scopes = (args.scope,) if args.scope else SCOPES
        print(f"interpreting run_date={run_date} model={args.model} "
              f"temperature={args.temperature} seed={args.seed} "
              f"context_mode={args.context_mode}",
              file=sys.stderr)
        results = synthesize_run(conn, run_date, args.model,
                                 temperature=args.temperature, seed=args.seed,
                                 scopes=scopes, fields=fields,
                                 context_mode=args.context_mode)
        for r in results:
            if r["skipped"]:
                print(f"  {r['scope']}: skipped (empty lane)", file=sys.stderr)
            elif r.get("refused"):
                est = r["estimate"]
                cost = ("unpriced" if est["est_cost_usd"] is None
                        else f"${est['est_cost_usd']:.4f}")
                print(f"  {r['scope']}: refused ({est['reason']}) - "
                      f"est {cost} vs cap ${est['cap_usd']:.2f} "
                      f"({est['row_count']} rows, ~{est['est_input_tokens']} in tokens)",
                      file=sys.stderr)
            else:
                print(f"  {r['scope']}: written "
                      f"({r['input_tokens']} in / {r['output_tokens']} out "
                      f"tokens, seed_applied={r['seed_applied']})",
                      file=sys.stderr)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
