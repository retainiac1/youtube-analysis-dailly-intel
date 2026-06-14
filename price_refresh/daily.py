"""Unattended daily price-refresh job: extract -> classify -> auto-apply / stage.

Ties the Phase 0 storage + validation layers and the Phase 1 extraction agent together
under the frozen contract. DETECT-AND-STAGE, never commit-on-judgment: a small sane
change auto-applies (a new model_prices window), a large sane change stages a pending
proposal for one-click human review, and bad data is rejected and logged.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field

import config
import db
import llm
from price_refresh import extract, store, validate, validators

# The llm_invocations scope under which extraction calls are logged (so their cost
# flows into the spend panel like any other LLM run).
_EXTRACTION_SCOPE = "price_extraction"

# Best-effort fixed seed for repeatable extraction (seedless providers drop it; see the
# capability gating in llm.generate). Low temperature does the real variance-reduction.
EXTRACTION_SEED = 7

# Output-token ceiling for an extraction call. JSON-with-citations for several models
# needs more room than an interpretation summary, so this is NOT the model's stored cap.
EXTRACTION_MAX_TOKENS = 2000

# app_preferences key holding the user-selected extraction model (a plain provider:model
# string). The ONE shared read path below is used by BOTH run_daily and the dashboard, so
# the unattended cron and the manual Run button never run a different model.
ACTIVE_MODEL_PREF = "extraction_model"


def active_extraction_model(conn: sqlite3.Connection) -> str:
    """The extraction model in effect: the persisted app_preferences choice if set, else
    the settings.toml EXTRACTION_MODEL seed default. run_daily (cron) and the dashboard
    both resolve the model through here, so they can never diverge."""
    return db.get_preference(conn, ACTIVE_MODEL_PREF) or config.EXTRACTION_MODEL


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
    flag: str | None = None              # cross-check cell flag (provenance/display)
    validator_value: float | None = None  # the validator's per-1M value for this field


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


def _cell_flag(value: float, validator_value: float | None, lookup_reason: str, *,
               tolerance_pct: float, review_band_pct: float) -> str:
    """The cross-check cell flag for one scraped field. NO_KEY_MAPPING (our config gap)
    and a missing validator value (no validator entry / none answered) are kept DISTINCT
    from a real match/drift/mismatch so a mapping gap never hides as a benign unverified
    and 'the validator does not carry it' never reads as a real conflict."""
    if lookup_reason == validators.NO_KEY_MAPPING:
        return validate.CELL_NO_KEY_MAPPING
    if validator_value is None:
        return validate.CELL_UNVERIFIED
    return validate.classify_cell(value, validator_value,
                                  tolerance_pct=tolerance_pct,
                                  review_band_pct=review_band_pct)


def _resolve_field(scraped, last: float, validator_value: float | None,
                   lookup_reason: str, *, lo: float, hi: float, threshold: float,
                   tolerance_pct: float, review_band_pct: float) -> _FieldDecision:
    """Resolve ONE (model, field). The SCRAPE is the value-of-record (never the
    validator); the validator only gates confidence via the cell flag. `scraped` is
    (value, quote, source_url) from the one provider page, or None when the scrape gave
    no value. Magnitude/unit errors REJECT regardless of the validator; a real move
    AUTO-applies only when the validator MATCHes, else it stages for review (a disputed
    or unconfirmable number is never auto-applied, and the validator's number is never
    adopted)."""
    if scraped is None:
        return _FieldDecision(_NODATA, validator_value=validator_value)
    value, quote, url = scraped
    flag = _cell_flag(value, validator_value, lookup_reason,
                      tolerance_pct=tolerance_pct, review_band_pct=review_band_pct)

    cls = validate.classify_price(value, last, lo=lo, hi=hi, threshold=threshold)
    if cls.outcome == validate.REJECT:
        return _FieldDecision(_REJECT, reason=cls.reason, flag=flag,
                              validator_value=validator_value)
    if cls.outcome == validate.REVIEW:
        return _FieldDecision(_REVIEW, value, cls.pct, cls.direction, quote, url,
                              flag=flag, validator_value=validator_value)
    # classify == AUTO (a small move or no move). store.value_changed is THE shared
    # "did it move?" tolerance (== apply's idempotency guard).
    if not store.value_changed(value, last):
        return _FieldDecision(_NOOP, value, flag=flag,
                              validator_value=validator_value)
    # A real (small) move auto-applies ONLY when the validator confirms it (MATCH);
    # drift / mismatch / unverified / no-key-mapping all stage for a human instead.
    if flag == validate.CELL_MATCH:
        return _FieldDecision(_AUTO, value, cls.pct, cls.direction, quote, url,
                              flag=flag, validator_value=validator_value)
    return _FieldDecision(_REVIEW, value, cls.pct, cls.direction, quote, url,
                          flag=flag, validator_value=validator_value)


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
                                            d.quote, d.source_url, flag=d.flag,
                                            validator_value=d.validator_value)


def _stage(conn: sqlite3.Connection, model: str, fld: str, old_value: float,
           d: _FieldDecision, now: str) -> None:
    def _txn() -> None:
        with db.transaction(conn):
            store.upsert_proposal(
                conn, model=model, field=fld, old_value=old_value, new_value=d.value,
                pct_change=d.pct, direction=d.direction, quote=d.quote,
                source_url=d.source_url, now=now)
    db.run_with_db_retry(_txn)


def _source_outcomes(results, feed) -> list:
    """The 'Sources this run' provenance: every URL actually attempted (one scrape per
    provider + every validator attempt, in order). A failed scrape's error is captured
    here, fixing the old silent drop. BOTH validator rows appear when the fallback fired,
    so a failed primary validator is never hidden."""
    out = []
    for r in results:
        out.append({"role": "scrape", "provider": r.provider, "url": r.source_url,
                    "display_url": r.source_url, "ok": r.ok,
                    "reason": None if r.ok else r.error})
    for o in feed.outcomes:
        out.append({"role": "validator", "name": o.name, "url": o.url,
                    "display_url": o.display_url, "ok": o.ok, "status": o.status,
                    "reason": o.error, "fetched_at": o.fetched_at})
    return out


def run_daily(conn: sqlite3.Connection, *, fetch=None, generate_text=None,
              now: str | None = None, source_map: dict | None = None,
              models=None, feed=None, model_keys: dict | None = None) -> RunSummary:
    """The unattended daily job: scrape the ONE official page per provider, cross-check
    each (model, field) against a third-party validator feed, auto-apply small
    validator-MATCHed changes as new model_prices windows, stage large / disputed /
    unverified / inverted ones as pending proposals, reject unit errors, persist a
    cross-check provenance snapshot, and emit a RunSummary. The scrape is the
    value-of-record; the validator only gates confidence. Idempotent: apply no-ops on an
    unchanged price and the pending index dedups, so a same-day re-run neither
    double-applies nor stacks (the cross-check snapshot is append history)."""
    if now is None:
        now = config.now_local_iso()
    run_date = now[:10]
    if source_map is None:
        source_map = config.PRICE_SOURCES
    if models is None:
        models = store.extractable_models(conn)
    pr = config.PRICE_REFRESH
    pv = config.PRICE_VALIDATION
    if model_keys is None:
        model_keys = pv["model_keys"]
    ua = pr["user_agent"]
    if fetch is None:
        # Bind the configured browser UA so provider pages that 403 a default httpx
        # client are reachable. The UA lives in config, never a literal in the fetcher.
        fetch = lambda u: extract.fetch_page(u, user_agent=ua)  # noqa: E731
    if generate_text is None:
        generate_text = make_generate_text(
            conn, active_extraction_model(conn), run_date)
    if feed is None:
        feed = validators.fetch_validator(
            litellm_url=pv["litellm_url"], openrouter_url=pv["openrouter_url"],
            litellm_display_url=pv["litellm_display_url"],
            openrouter_display_url=pv["openrouter_display_url"],
            user_agent=ua, now=now)

    lo, hi = pr["magnitude_lo"], pr["magnitude_hi"]
    threshold = pr["delta_threshold"]
    tolerance_pct, review_band_pct = pv["tolerance_pct"], pv["review_band_pct"]

    results = extract.extract_prices(
        source_map, _group_by_provider(models), fetch=fetch,
        generate_text=generate_text)
    scrape_by_provider = {r.provider: r for r in results}

    # Silent-failure guard: a model with no model_keys entry for the answering validator
    # silently stops being cross-checked. Surface the full list loudly (the per-model
    # lookup also flags each as 'no key mapping').
    missing_maps = validators.check_map_completeness(models, model_keys, feed.name)
    if missing_maps:
        print(f"WARNING: validator '{feed.name}' has no model_keys entry for "
              f"{missing_maps}; those models are NOT cross-checked", file=sys.stderr)

    summary = RunSummary()
    cells: dict = {}
    for model in sorted(models):
        provider = model.split(":", 1)[0]
        scrape = scrape_by_provider.get(provider)
        payload = (scrape.prices
                   if scrape and scrape.ok and scrape.prices else None)
        vprices, vreason = validators.validator_lookup(feed, model, model_keys)

        active = db.active_price_window(conn, model)
        if active is None:
            print(f"WARNING: {model}: no active price window; skipping",
                  file=sys.stderr)
            summary.skipped += 1
            summary.errors.append((model, None, "no active window"))
            continue

        decisions: dict = {}
        for fld in ("input", "output"):
            scraped = None
            if payload and model in payload and fld in payload[model]:
                e = payload[model]
                scraped = (e[fld], e.get("quote"), e.get("source_url"))
            vval = vprices[fld] if vprices else None
            decisions[fld] = _resolve_field(
                scraped, active[f"{fld}_per_1m"], vval, vreason, lo=lo, hi=hi,
                threshold=threshold, tolerance_pct=tolerance_pct,
                review_band_pct=review_band_pct)
        _inversion_post_pass(decisions, active)

        cells[model] = {
            fld: {"scraped": decisions[fld].value if decisions[fld].outcome != _NODATA
                  else None,
                  "validator": decisions[fld].validator_value,
                  "flag": decisions[fld].flag}
            for fld in ("input", "output")
        }

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

    # Persist ONE cross-check snapshot for this run (append history). Written whole in a
    # single transaction so a crash mid-run cannot leave a half-written snapshot /prices
    # would render as truth.
    outcomes = _source_outcomes(results, feed)

    def _write_snapshot() -> None:
        with db.transaction(conn):
            db.insert_cross_check(
                conn, run_at=now, validator_name=feed.name,
                source_outcomes=json.dumps(outcomes), cells=json.dumps(cells))
    db.run_with_db_retry(_write_snapshot)

    print(f"[price-refresh] applied={summary.applied} staged={summary.staged} "
          f"rejected={summary.rejected} skipped={summary.skipped} "
          f"validator={feed.name}", file=sys.stderr)
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
