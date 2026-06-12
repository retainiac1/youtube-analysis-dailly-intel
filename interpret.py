"""Interpretation generator core (Phase 2).

Reads a ranked lane, builds a prompt, calls the single `llm.generate` seam, and
persists the result: one canonical row per (run_date, scope) in `interpretations`
plus one append-only row in `llm_invocations` recording the full parameter set.
Importable by the Phase 2 CLI and the Phase 3 dashboard endpoint, so the button
is a thin wrapper rather than a reimplementation.

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
import sys
import time

import db
from config import DB_PATH, ConfigError, VALID_BUCKETS, now_local_iso, validate_config
from llm import generate, LLMError

# Lane order matches compute_rankings keys: "overall" plus VALID_BUCKETS. Derived
# from the single source of truth (config.VALID_BUCKETS) so adding a bucket does not
# require editing two places; sorted() makes the order deterministic.
SCOPES = ("overall",) + tuple(sorted(VALID_BUCKETS))

# A sane default valid for every provider's temperature range (Anthropic 0-1,
# others 0-2). The adapter validates against its own range downstream.
DEFAULT_TEMPERATURE = 1.0


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


def synthesize_lane(conn, run_date: str, scope: str, model: str, *,
                    temperature: float, seed, think=None, filter=None,
                    fields=None) -> dict:
    """Synthesize one lane: fetch it, and if non-empty call generate() then
    persist. An empty lane skips the LLM call and writes no rows.

    `model` is the canonical "provider:model" string. Its capability flags
    (supports_temperature / supports_seed / is_reasoning) are read from the `models`
    table here — the one place holding a connection — and threaded into generate(),
    keeping llm.py DB-free. An unregistered model raises LLMError (the dashboard turns
    it into a clean 400), since its capabilities are unknown. `think` is the per-run
    reasoning toggle; generate() forwards it only for a reasoning-capable model on a
    think-honoring provider, otherwise the applied value (result.think_applied) is
    None. `fields` selects which per-video signals the prompt carries (normalized in
    build_prompt; None → the default set). Network happens BEFORE the transaction
    opens. The two writes run in ONE transaction in the pinned order (log_invocation,
    then upsert_interpretation), wrapped in run_with_db_retry for the lock case."""
    rows = db.fetch_lane(conn, run_date, scope)
    if not rows:
        return {"scope": scope, "skipped": True}

    model_row = db.fetch_model(conn, model)
    if model_row is None:
        raise LLMError(
            f"model {model!r} is not in the registry; add it on the /models page"
        )

    prompt = build_prompt(run_date, scope, rows, fields)
    # Time the network call: this is the generator "run time" we persist and show.
    # monotonic() is immune to wall-clock adjustments. The DB write below is trivial
    # and deliberately excluded so the figure reflects the model, not SQLite.
    start = time.monotonic()
    result = generate(model, prompt, temperature=temperature, seed=seed,
                      supports_temperature=bool(model_row["supports_temperature"]),
                      supports_seed=bool(model_row["supports_seed"]),
                      think=think,
                      is_reasoning=bool(model_row["is_reasoning"]))
    duration_ms = int((time.monotonic() - start) * 1000)
    now = now_local_iso()

    def _write():
        with db.transaction(conn):
            db.log_invocation(conn, run_date, scope, model, temperature,
                              result.seed_applied, filter, result.input_tokens,
                              result.output_tokens, now, duration_ms=duration_ms)
            # Persist the APPLIED parameters: the requested temperature, the seed that
            # actually governed (result.seed_applied), and the applied think value
            # (result.think_applied — NULL when think did not apply, 0/1 when it did),
            # mirroring what log_invocation records.
            db.upsert_interpretation(conn, run_date, scope, result.text, model,
                                     now, temperature=temperature,
                                     seed=result.seed_applied,
                                     think=result.think_applied)

    db.run_with_db_retry(_write)

    return {
        "scope": scope,
        "skipped": False,
        "text": result.text,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "seed_applied": result.seed_applied,
        "think_applied": result.think_applied,
        "thinking": result.thinking,
        "duration_ms": duration_ms,
    }


def synthesize_run(conn, run_date: str, model: str, *, temperature: float,
                   seed, scopes=SCOPES, fields=None) -> list:
    """Synthesize every scope for a run sequentially with one model + parameter
    set (and one prompt-field selection), returning the per-scope outcomes
    (written or skipped-empty)."""
    return [
        synthesize_lane(conn, run_date, scope, model,
                        temperature=temperature, seed=seed, fields=fields)
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
              f"temperature={args.temperature} seed={args.seed}",
              file=sys.stderr)
        results = synthesize_run(conn, run_date, args.model,
                                 temperature=args.temperature, seed=args.seed,
                                 scopes=scopes, fields=fields)
        for r in results:
            if r["skipped"]:
                print(f"  {r['scope']}: skipped (empty lane)", file=sys.stderr)
            else:
                print(f"  {r['scope']}: written "
                      f"({r['input_tokens']} in / {r['output_tokens']} out "
                      f"tokens, seed_applied={r['seed_applied']})",
                      file=sys.stderr)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
