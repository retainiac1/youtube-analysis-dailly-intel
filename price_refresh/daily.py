"""Unattended daily price-refresh job: extract -> classify -> auto-apply / stage.

Ties the Phase 0 storage + validation layers and the Phase 1 extraction agent together
under the frozen contract. DETECT-AND-STAGE, never commit-on-judgment: a small sane
change auto-applies (a new model_prices window), a large sane change stages a pending
proposal for one-click human review, and bad data is rejected and logged.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from dataclasses import dataclass, field

import config
import db
import llm
from price_refresh import extract, store, validate

# The llm_invocations scope under which extraction calls are logged (so their cost
# flows into the spend panel like any other LLM run).
_EXTRACTION_SCOPE = "price_extraction"

# Best-effort fixed seed for repeatable extraction (seedless providers drop it; see the
# capability gating in llm.generate). Low temperature does the real variance-reduction.
EXTRACTION_SEED = 7

# Output-token ceiling for an extraction call. JSON-with-citations for several models
# needs more room than an interpretation summary, so this is NOT the model's stored cap.
EXTRACTION_MAX_TOKENS = 2000


def make_generate_text(conn: sqlite3.Connection, model: str, run_date: str):
    """Return the `(prompt) -> str` closure extract.extract_source expects, bound to
    `model`. It calls llm.generate (low temp, best-effort seed), logs each call to
    llm_invocations (so extraction cost is tracked), and converts llm.LLMError ->
    extract.ExtractionError per the extract_source failure contract. Capability flags
    are read once from the models table; an unknown model raises."""
    flags = db.fetch_model(conn, model)
    if flags is None:
        raise ValueError(f"unknown extraction model {model!r}")

    def generate_text(prompt: str) -> str:
        start = time.monotonic()
        try:
            result = llm.generate(
                model, prompt, temperature=0.0, seed=EXTRACTION_SEED,
                supports_temperature=bool(flags["supports_temperature"]),
                supports_seed=bool(flags["supports_seed"]),
                is_reasoning=bool(flags["is_reasoning"]),
                max_tokens=EXTRACTION_MAX_TOKENS)
        except llm.LLMError as e:
            raise extract.ExtractionError(str(e)) from e
        duration_ms = int((time.monotonic() - start) * 1000)
        now = config.now_local_iso()

        def _log() -> None:
            with db.transaction(conn):
                db.log_invocation(
                    conn, run_date, _EXTRACTION_SCOPE, model, 0.0,
                    result.seed_applied, None, result.input_tokens,
                    result.output_tokens, now, duration_ms=duration_ms)

        db.run_with_db_retry(_log)
        return result.text

    return generate_text


# Per-field outcomes after classification (one per model/field).
_AUTO, _REVIEW, _REJECT, _NOOP, _NODATA = "auto", "review", "reject", "noop", "nodata"


@dataclass
class _FieldDecision:
    outcome: str
    value: float | None = None
    pct: float | None = None
    direction: str | None = None
    quote: str | None = None
    source_url: str | None = None
    reason: str = ""


@dataclass
class RunSummary:
    """The counts a daily run emits. `errors` carries (model, field, reason) tuples for
    rejects and skips so a human can see WHY, not just how many."""

    applied: int = 0
    staged: int = 0
    rejected: int = 0
    skipped: int = 0
    errors: list = field(default_factory=list)


def _group_by_provider(models) -> dict:
    """{provider: {provider:model, ...}} — the per-source expected key set."""
    grouped: dict = {}
    for model in models:
        grouped.setdefault(model.split(":", 1)[0], set()).add(model)
    return grouped


def _resolve_field(vals: list, last: float, *, lo: float, hi: float,
                   cross_tol: float, threshold: float) -> _FieldDecision:
    """Resolve ONE (model, field): cross-source first (or single-source), then the
    magnitude+delta gates. Per-field only — the cross-field inversion is a separate pass.
    Each `vals` entry is (value, quote, source_url) from one ok source."""
    if not vals:
        return _FieldDecision(_NODATA)
    if len(vals) >= 2:
        (a, qa, ua), (b, qb, ub) = vals[0], vals[1]
        cross = validate.check_cross_source(a, b, last, cross_tol)
        if cross.outcome == validate.CROSS_CONFLICT:
            return _FieldDecision(_REJECT, reason="cross-source conflict")
        value = cross.value
        if cross.outcome == validate.CROSS_AGREE:
            quote, url, uncross = qa, ua, False
        else:   # passthrough: cite the source that moved
            quote, url = (qa, ua) if cross.moved_source == "a" else (qb, ub)
            uncross = True
    else:
        value, quote, url = vals[0]
        uncross = True

    cls = validate.classify_price(value, last, lo=lo, hi=hi, threshold=threshold)
    if cls.outcome == validate.REJECT:
        return _FieldDecision(_REJECT, reason=cls.reason)
    if cls.outcome == validate.REVIEW:
        return _FieldDecision(_REVIEW, value, cls.pct, cls.direction, quote, url)
    # AUTO: a no-move is a no-op; a small but uncross-checked move still needs a human.
    # store.value_changed is THE shared "did it move?" tolerance (== apply's idempotency
    # guard), distinct from cross_tol (~5% agreement): a real 4% change IS a move.
    if not store.value_changed(value, last):
        return _FieldDecision(_NOOP, value)
    if uncross:
        return _FieldDecision(_REVIEW, value, cls.pct, cls.direction, quote, url)
    return _FieldDecision(_AUTO, value, cls.pct, cls.direction, quote, url)


def _inversion_post_pass(decisions: dict, active: sqlite3.Row) -> None:
    """Single cross-field pass BEFORE compose: if the would-be (input, output) is
    inverted (output <= input), demote every currently-AUTO (moved) field to REVIEW —
    so an inverted moved field is staged, never both applied and staged."""
    def would_be(fld: str):
        d = decisions[fld]
        if d.outcome in (_AUTO, _REVIEW):
            return d.value
        if d.outcome == _NOOP:
            return active[f"{fld}_per_1m"]
        return None   # reject / nodata -> no concrete value
    inp, out = would_be("input"), would_be("output")
    if inp is None or out is None or out > inp:
        return
    for fld in ("input", "output"):
        d = decisions[fld]
        if d.outcome == _AUTO:
            decisions[fld] = _FieldDecision(_REVIEW, d.value, d.pct, d.direction,
                                            d.quote, d.source_url)


def _stage(conn: sqlite3.Connection, model: str, fld: str, old_value: float,
           d: _FieldDecision, now: str) -> None:
    def _txn() -> None:
        with db.transaction(conn):
            store.upsert_proposal(
                conn, model=model, field=fld, old_value=old_value, new_value=d.value,
                pct_change=d.pct, direction=d.direction, quote=d.quote,
                source_url=d.source_url, now=now)
    db.run_with_db_retry(_txn)


def run_daily(conn: sqlite3.Connection, *, fetch=None, generate_text=None,
              now: str | None = None, source_map: dict | None = None,
              models=None) -> RunSummary:
    """The unattended daily job: extract both sources per provider, classify each
    (model, field), auto-apply small cross-checked changes as new model_prices windows,
    stage large/uncross-checked/inverted ones as pending proposals, reject bad data, and
    emit a RunSummary. Idempotent: apply no-ops on an unchanged price and the pending
    index dedups proposals, so a same-day re-run neither double-applies nor stacks."""
    if now is None:
        now = config.now_local_iso()
    run_date = now[:10]
    if source_map is None:
        source_map = config.PRICE_SOURCES
    if models is None:
        models = store.extractable_models(conn)
    if fetch is None:
        fetch = extract.fetch_page
    if generate_text is None:
        generate_text = make_generate_text(conn, config.EXTRACTION_MODEL, run_date)

    pr = config.PRICE_REFRESH
    lo, hi = pr["magnitude_lo"], pr["magnitude_hi"]
    cross_tol, threshold = pr["cross_tol"], pr["delta_threshold"]

    results = extract.extract_prices(
        source_map, _group_by_provider(models), fetch=fetch,
        generate_text=generate_text)
    ok_by_provider: dict = {}
    for r in results:
        if r.ok:
            ok_by_provider.setdefault(r.provider, []).append(r.prices)

    summary = RunSummary()
    for model in sorted(models):
        provider = model.split(":", 1)[0]
        active = db.active_price_window(conn, model)
        if active is None:
            print(f"WARNING: {model}: no active price window; skipping",
                  file=sys.stderr)
            summary.skipped += 1
            summary.errors.append((model, None, "no active window"))
            continue

        payloads = [p for p in ok_by_provider.get(provider, []) if model in p]
        decisions: dict = {}
        for fld in ("input", "output"):
            vals = [(p[model][fld], p[model]["quote"], p[model]["source_url"])
                    for p in payloads]
            decisions[fld] = _resolve_field(
                vals, active[f"{fld}_per_1m"], lo=lo, hi=hi, cross_tol=cross_tol,
                threshold=threshold)
        _inversion_post_pass(decisions, active)

        # Compose ONE window: AUTO fields take their value, others carry forward.
        if any(decisions[f].outcome == _AUTO for f in ("input", "output")):
            target_in = (decisions["input"].value
                         if decisions["input"].outcome == _AUTO
                         else active["input_per_1m"])
            target_out = (decisions["output"].value
                          if decisions["output"].outcome == _AUTO
                          else active["output_per_1m"])
            if store.apply_price(conn, model=model, input_per_1m=target_in,
                                 output_per_1m=target_out, now=now) is not None:
                summary.applied += 1

        for fld in ("input", "output"):
            d = decisions[fld]
            if d.outcome == _REVIEW:
                _stage(conn, model, fld, active[f"{fld}_per_1m"], d, now)
                summary.staged += 1
            elif d.outcome == _REJECT:
                print(f"WARNING: {model}.{fld} rejected: {d.reason}", file=sys.stderr)
                summary.rejected += 1
                summary.errors.append((model, fld, d.reason))
            elif d.outcome == _NODATA:
                summary.skipped += 1
                summary.errors.append((model, fld, "no source data"))

    print(f"[price-refresh] applied={summary.applied} staged={summary.staged} "
          f"rejected={summary.rejected} skipped={summary.skipped}", file=sys.stderr)
    return summary


def main() -> None:
    """CLI entry: validate config, open the DB (default config.DB_PATH; point --db-path
    at a scratch copy to run safely against live sources), run the daily job. Scheduling
    is external (cron / launchd / Cowork task)."""
    parser = argparse.ArgumentParser(description="Daily price-refresh agent.")
    parser.add_argument(
        "--db-path", default=config.DB_PATH,
        help="SQLite DB path (default: config.DB_PATH; use a scratch copy to test live).")
    args = parser.parse_args()

    try:
        config.validate_config()
    except config.ConfigError as e:
        print(f"ERROR: invalid config: {e}", file=sys.stderr)
        sys.exit(1)

    db.init_db(args.db_path)
    conn = db.get_connection(args.db_path)
    try:
        run_daily(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
