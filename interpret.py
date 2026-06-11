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
from llm import generate

# Lane order matches compute_rankings keys: "overall" plus VALID_BUCKETS. Derived
# from the single source of truth (config.VALID_BUCKETS) so adding a bucket does not
# require editing two places; sorted() makes the order deterministic.
SCOPES = ("overall",) + tuple(sorted(VALID_BUCKETS))

# A sane default valid for every provider's temperature range (Anthropic 0-1,
# others 0-2). The adapter validates against its own range downstream.
DEFAULT_TEMPERATURE = 1.0


def build_prompt(run_date: str, scope: str, rows: list) -> str:
    """Assemble the synthesis prompt from a lane's ranked rows. States the scope
    and the EXACT row count so the model cannot overstate a trend from a thin set,
    and asks for 3-4 sentences. Single source of truth for prompt text.

    `rows` are fetch_lane rows (sqlite3.Row); fetch_lane LEFT JOINs videos, so
    title/channel/views may be NULL when the video row is missing. Such fields
    are omitted from a line rather than emitted as the literal "None"."""
    lines = []
    for row in rows:
        parts = [f"#{row['rank']}"]
        if row["title"] is not None:
            parts.append(f'"{row["title"]}"')
        if row["channel_title"] is not None:
            parts.append(f"by {row['channel_title']}")
        if row["view_count"] is not None:
            parts.append(f"{row['view_count']} views")
        if row["views_to_subs_ratio"] is not None:
            parts.append(f"views/subs ratio {row['views_to_subs_ratio']}")
        lines.append(" — ".join(parts))

    body = "\n".join(lines)
    return (
        f"You are analyzing the '{scope}' leaderboard for the run dated "
        f"{run_date}. It contains exactly {len(rows)} ranked short-form "
        f"video(s), listed below by rank.\n\n"
        f"{body}\n\n"
        f"Write a 3-4 sentence summary of what stands out in this lane. Base "
        f"every claim only on these {len(rows)} rows; do not infer a broader "
        f"trend than {len(rows)} video(s) can support."
    )


def synthesize_lane(conn, run_date: str, scope: str, model: str, *,
                    temperature: float, seed, filter=None) -> dict:
    """Synthesize one lane: fetch it, and if non-empty call generate() then
    persist. An empty lane skips the LLM call and writes no rows.

    `model` is the canonical "provider:model" string, passed straight to
    generate(). Network happens BEFORE the transaction opens. The two writes run
    in ONE transaction in the pinned order (log_invocation, then
    upsert_interpretation), wrapped in run_with_db_retry for the dashboard-lock
    case."""
    rows = db.fetch_lane(conn, run_date, scope)
    if not rows:
        return {"scope": scope, "skipped": True}

    prompt = build_prompt(run_date, scope, rows)
    # Time the network call: this is the generator "run time" we persist and show.
    # monotonic() is immune to wall-clock adjustments. The DB write below is trivial
    # and deliberately excluded so the figure reflects the model, not SQLite.
    start = time.monotonic()
    result = generate(model, prompt, temperature=temperature, seed=seed)
    duration_ms = int((time.monotonic() - start) * 1000)
    now = now_local_iso()

    def _write():
        with db.transaction(conn):
            db.log_invocation(conn, run_date, scope, model, temperature,
                              result.seed_applied, filter, result.input_tokens,
                              result.output_tokens, now, duration_ms=duration_ms)
            db.upsert_interpretation(conn, run_date, scope, result.text, model,
                                     now)

    db.run_with_db_retry(_write)

    return {
        "scope": scope,
        "skipped": False,
        "text": result.text,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "seed_applied": result.seed_applied,
        "duration_ms": duration_ms,
    }


def synthesize_run(conn, run_date: str, model: str, *, temperature: float,
                   seed, scopes=SCOPES) -> list:
    """Synthesize every scope for a run sequentially with one model + parameter
    set, returning the per-scope outcomes (written or skipped-empty)."""
    return [
        synthesize_lane(conn, run_date, scope, model,
                        temperature=temperature, seed=seed)
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
    args = parser.parse_args(argv)

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
                                 scopes=scopes)
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
