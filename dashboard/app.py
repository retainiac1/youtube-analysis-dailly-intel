"""Read API for the daily-intel dashboard.

Read-only over swipefile.db: JSON endpoints (leaderboard, filters, quota,
interpretation, and the Phase 2 change-over-time charts) plus the static frontend.
Imports only db.py and config.py (both stdlib-only); never swipefile.py. Every DB
read goes through db.run_with_db_retry so a pipeline write-lock degrades to a
short retry rather than a 500.
"""

import json
import os
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

# Repo root holds config.py and db.py. Put it on sys.path so this app imports them
# whether launched via `uvicorn dashboard.app:app` or as a package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi import Depends, FastAPI, HTTPException, Query  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
import interpret  # noqa: E402
import llm  # noqa: E402
from dashboard import discovery  # noqa: E402
from price_refresh import daily, store  # noqa: E402

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# The three leaderboard lanes. health/habit are rankings.bucket values and also
# videos.buckets members; overall is the whole-pool lane (a rankings.bucket
# value, not a videos.buckets member). The lane maps directly to rankings.bucket.
VALID_LANES = {"health", "habit", "overall"}

@asynccontextmanager
async def _lifespan(app: "FastAPI"):
    """Ensure the schema exists and is migrated to the current version on startup
    (idempotent + additive; offline-seeds the baseline models). The dashboard
    otherwise relies on the pipeline having run init_db, so launching it standalone
    against an un-migrated DB would 500 on every registry read (the dropdown, spend,
    /api/models). This makes the dashboard self-sufficient and never-broken. The DB
    path is resolved the same way every request resolves it (DASHBOARD_DB_PATH or
    config.DB_PATH)."""
    db.init_db(get_db_path())
    yield


app = FastAPI(title="daily-intel dashboard", docs_url="/api/docs",
              lifespan=_lifespan)


def _lane_to_bucket(lane: str) -> str:
    """Validate a lane query param and return the rankings.bucket it maps to
    (identity). A bad value is a 422, mirroring FastAPI's own param validation."""
    if lane not in VALID_LANES:
        raise HTTPException(
            status_code=422,
            detail=f"lane must be one of {sorted(VALID_LANES)}",
        )
    return lane


def _allowed_models(conn: sqlite3.Connection) -> list[str]:
    """The canonical 'provider:model' strings the generate controls may offer: the
    `models` registry rows the dropdown reader returns (enabled, not deleted, with a
    current price) whose provider has a working adapter. db.fetch_dropdown_models is
    the single source; the SUPPORTED_PROVIDERS guard (via llm.split_model, the ONE
    splitter) drops a hand-added row for an unsupported provider rather than offering
    it and then failing at generate(). fetch_dropdown_models already orders by
    model, so the dropdown is stable."""
    out = []
    for row in db.fetch_dropdown_models(conn):
        try:
            provider, _ = llm.split_model(row["model"])
        except llm.LLMError:
            continue
        if provider in llm.SUPPORTED_PROVIDERS:
            out.append(row["model"])
    return out


def get_db_path() -> str:
    """Production DB path. Honors DASHBOARD_DB_PATH when set (used to point a smoke
    run at a throwaway copy, so the live seed is never read or written by a smoke);
    otherwise resolves config.DB_PATH against the repo root so it is independent of
    the process CWD. Tests override this via app.dependency_overrides directly, so
    they never depend on the env var. When the var is unset, behavior is exactly
    the prior default."""
    override = os.environ.get("DASHBOARD_DB_PATH")
    if override:
        return override
    return str(_REPO_ROOT / config.DB_PATH)


def get_conn(db_path: str = Depends(get_db_path)):
    """Per-request connection: open via db.get_connection, yield, close in finally.
    FastAPI runs the post-yield finally after the response is sent.

    check_same_thread=False because FastAPI may create this connection on one
    threadpool worker and run the endpoint on another (concurrent requests, e.g.
    the Trends page firing several chart endpoints at once). The connection is
    per-request and used serially, so relaxing the same-thread check is safe."""
    conn = db.get_connection(db_path, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


# --- API routes (registered BEFORE the static mount, which is greedy at /) ----

@app.get("/api/runs")
def api_runs(conn: sqlite3.Connection = Depends(get_conn)):
    """Available rankings run_dates, most recent first."""
    rows = db.run_with_db_retry(lambda: db.fetch_run_dates(conn))
    return {"run_dates": [row["run_date"] for row in rows]}


@app.get("/api/rankings")
def api_rankings(
    run_date: str = Query(..., min_length=1),
    bucket: str = Query(..., min_length=1),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """One lane's leaderboard rows for (run_date, bucket). An empty lane is a
    clean empty list, not an error."""
    rows = db.run_with_db_retry(lambda: db.fetch_lane(conn, run_date, bucket))
    return {
        "run_date": run_date,
        "bucket": bucket,
        "rows": [dict(row) for row in rows],
    }


@app.get("/api/quota")
def api_quota(conn: sqlite3.Connection = Depends(get_conn)):
    """Units used today (Pacific) vs the effective cap. The Pacific date is
    derived only via config.pacific_date(); run_date (Eastern) is never used here."""
    pacific_date = config.pacific_date()
    units_used = db.run_with_db_retry(
        lambda: db.get_units_used(conn, pacific_date)
    )
    cap = config.DAILY_QUOTA_LIMIT - config.SAFETY_BUFFER
    return {
        "pacific_date": pacific_date,
        "units_used": units_used,
        "cap": cap,
        "remaining": cap - units_used,
    }


@app.get("/api/interpretation")
def api_interpretation(
    run_date: str = Query(..., min_length=1),
    scope: str = Query(..., min_length=1),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Stored interpretation text for (run_date, scope), plus the applied provenance
    (temperature/seed/think — NULL where not applicable) so the view can show how the
    run was produced. Absent text renders as a clean empty state (status 200, empty
    string), never an error. The thinking CONTENT is not persisted, so it is absent
    here; it rides back only on the fresh /api/interpret response."""
    row = db.run_with_db_retry(
        lambda: db.fetch_interpretation(conn, run_date, scope)
    )
    if row is None:
        return {
            "run_date": run_date,
            "scope": scope,
            "text": "",
            "model": None,
            "generated_at": None,
            "duration_ms": None,
            "temperature": None,
            "seed": None,
            "think": None,
        }
    return {
        "run_date": run_date,
        "scope": scope,
        "text": row["text"],
        "model": row["model"],
        "generated_at": row["generated_at"],
        "duration_ms": row["duration_ms"],
        "temperature": row["temperature"],
        "seed": row["seed"],
        "think": row["think"],
    }


@app.get("/api/filter-options")
def api_filter_options(
    run_date: str = Query(..., min_length=1),
    lane: str = Query(..., min_length=1),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Filter option lists and range bounds for ONE lane's ranked videos. Scoped
    to (run_date, lane), NOT global: the rail only offers values present in the
    current board. The frontend re-fetches this whenever the run or lane changes.
    An empty lane returns empty lists / null bounds, not an error."""
    bucket = _lane_to_bucket(lane)
    options = db.run_with_db_retry(
        lambda: db.fetch_filter_options(conn, run_date, bucket)
    )
    return {"run_date": run_date, "lane": lane, **options}


@app.get("/api/library")
def api_library(
    run_date: str = Query(..., min_length=1),
    lane: str = Query(..., min_length=1),
    matched_query: list[str] = Query(default=[]),
    channel_id: list[str] = Query(default=[]),
    country: list[str] = Query(default=[]),
    duration_band: list[str] = Query(default=[]),
    view_min: int | None = Query(default=None),
    view_max: int | None = Query(default=None),
    ratio_min: float | None = Query(default=None),
    ratio_max: float | None = Query(default=None),
    published_after: str | None = Query(default=None),
    published_before: str | None = Query(default=None),
    first_seen_after: str | None = Query(default=None),
    first_seen_before: str | None = Query(default=None),
    starred_only: bool = Query(default=False),
    has_notes_only: bool = Query(default=False),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """The Phase 1 board: one lane's ranked leaderboard for the selected run,
    narrowed by the given filters. run_date and lane are required. An empty
    result is a clean empty list, not an error. (Distinct from /api/rankings,
    which is the narrow lane view retained for Phase 2 charts.)"""
    bucket = _lane_to_bucket(lane)
    rows = db.run_with_db_retry(
        lambda: db.fetch_library(
            conn,
            run_date=run_date,
            bucket=bucket,
            matched_queries=matched_query or None,
            channel_ids=channel_id or None,
            countries=country or None,
            duration_bands=duration_band or None,
            view_min=view_min,
            view_max=view_max,
            ratio_min=ratio_min,
            ratio_max=ratio_max,
            published_after=published_after,
            published_before=published_before,
            first_seen_after=first_seen_after,
            first_seen_before=first_seen_before,
            starred_only=starred_only,
            has_notes_only=has_notes_only,
        )
    )
    return {"run_date": run_date, "lane": lane, "count": len(rows), "rows": rows}


# Cap on tracked videos charted at once: matches the frontend's 5-video limit and
# bounds the snapshot query. A longer list is a client bug, so it is a 422.
MAX_TRACKED = 5


@app.get("/api/snapshots")
def api_snapshots(
    video_id: list[str] = Query(default=[]),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Per-video view trajectory + views/day velocity from stats_snapshots. Not
    lane-scoped (snapshots have no lane). An empty video_id list returns an empty
    series list; a requested video with no snapshots is returned with empty points
    and velocity, so the UI can name which tracked video has no data. A single-
    point series has no velocity pairs (empty velocity), not an error."""
    if len(video_id) > MAX_TRACKED:
        raise HTTPException(
            status_code=422,
            detail=f"at most {MAX_TRACKED} video_id values may be charted at once",
        )
    series_by_id = db.run_with_db_retry(
        lambda: db.fetch_snapshot_series(conn, video_id)
    )
    series = []
    for vid in video_id:
        entry = series_by_id.get(vid)
        points = entry["points"] if entry else []
        title = entry["title"] if entry else None
        series.append(
            {
                "video_id": vid,
                "title": title,
                "points": points,
                "velocity": db._velocity_points(points),
            }
        )
    return {"series": series}


@app.get("/api/rank-history")
def api_rank_history(
    lane: str = Query(..., min_length=1),
    video_id: list[str] = Query(default=[]),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """One lane's rank movement across ALL run_dates (the bump chart), plus the
    metric_value-over-time (views_to_subs_ratio) series. The run picker does NOT
    constrain this — a bump chart spans runs. Optionally restrict to tracked
    video_ids. An empty lane returns empty run_dates and series, not an error."""
    bucket = _lane_to_bucket(lane)
    data = db.run_with_db_retry(
        lambda: db.fetch_rank_history(conn, bucket, video_id or None)
    )
    return {"lane": lane, **data}


@app.get("/api/distribution")
def api_distribution(
    run_date: str = Query(..., min_length=1),
    lane: str = Query(..., min_length=1),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """View-count distribution histogram for one run's ranked lane, using the
    shared config.distribution_buckets so the buckets match the pipeline's tuning
    view exactly (swipefile.py is never imported). Buckets are returned in the
    canonical config.DISTRIBUTION_BUCKETS order. An empty lane returns all-zero
    counts and total 0, not an error."""
    bucket = _lane_to_bucket(lane)
    rows = db.run_with_db_retry(lambda: db.fetch_lane(conn, run_date, bucket))
    # Group each ranked video (with a real view_count) into its bucket, so the
    # histogram tooltip can list the videos behind each bar. Classify via the
    # shared config.distribution_bucket so the boundaries match the pipeline
    # exactly (never re-defined here, never importing swipefile).
    by_bucket: dict[str, list[dict]] = {label: [] for label in config.DISTRIBUTION_BUCKETS}
    total = 0
    for row in rows:
        vc = row["view_count"]
        if vc is None:
            continue
        total += 1
        by_bucket[config.distribution_bucket(vc)].append(
            {"title": row["title"], "view_count": vc}
        )
    for videos in by_bucket.values():
        videos.sort(key=lambda v: v["view_count"], reverse=True)
    return {
        "run_date": run_date,
        "lane": lane,
        "buckets": [
            {"label": label, "count": len(by_bucket[label]), "videos": by_bucket[label]}
            for label in config.DISTRIBUTION_BUCKETS
        ],
        "total": total,
    }


# --- Write routes (Phase 3) ---------------------------------------------------
# The ONLY mutations the dashboard performs: videos.user_notes and videos.starred
# (with starred_at). Both name user-owned columns only (db.set_user_notes /
# db.set_starred). Wrapping: run_with_db_retry OUTER, db.transaction INNER, so a
# pipeline write-lock rolls the write back and the retry re-runs the whole
# transaction. An UPDATE that matches no row (unknown id, e.g. a ghost ranking
# whose video row is missing) is a 404, never a silent no-op.

class NotesUpdate(BaseModel):
    user_notes: str


class StarUpdate(BaseModel):
    starred: bool


@app.put("/api/videos/{video_id}/notes")
def api_set_notes(
    video_id: str,
    body: NotesUpdate,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Overwrite one video's user_notes (full replace, last-write-wins). An empty
    string clears the note (matches the has_notes_only filter's '' contract)."""
    def write():
        with db.transaction(conn):
            return db.set_user_notes(conn, video_id, body.user_notes)

    if db.run_with_db_retry(write) == 0:
        raise HTTPException(status_code=404, detail=f"no video with id {video_id}")
    return {"video_id": video_id, "user_notes": body.user_notes}


@app.put("/api/videos/{video_id}/star")
def api_set_star(
    video_id: str,
    body: StarUpdate,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Set one video's starred flag. starred_at (Eastern ISO via config) is set on
    star and cleared on unstar."""
    starred_at = config.now_local_iso() if body.starred else None

    def write():
        with db.transaction(conn):
            return db.set_starred(conn, video_id, body.starred, starred_at)

    if db.run_with_db_retry(write) == 0:
        raise HTTPException(status_code=404, detail=f"no video with id {video_id}")
    return {"video_id": video_id, "starred": body.starred, "starred_at": starred_at}


# --- Interpretation generation (Phase 3 generator) ----------------------------
# The one outbound-LLM write the dashboard performs: trigger synthesis for the
# viewed lane via the Phase 2 core (interpret.synthesize_lane), which already does
# fetch-lane -> generate() (network) -> one transaction (log_invocation then
# upsert_interpretation) wrapped in run_with_db_retry. So this endpoint is a thin
# wrapper: validate, call the core, map llm.LLMError to a clean 400 (never a raw
# 500), and return the result. It does NOT add its own transaction/retry — the
# core owns that, and double-wrapping would nest transactions.
# This is the deliberate contract change amended into docs/dashboard-plan.MD;
# everything else in the dashboard stays read-only.

PROMPT_FIELDS_PREF = "prompt_fields"


def _stored_prompt_fields(conn: sqlite3.Connection) -> list[str]:
    """The persisted prompt-field selection, normalized, or the default set. The
    JSON parse is guarded: a missing OR malformed stored value (hand-edit, partial
    write) degrades to the default rather than 500-ing the page-load prepopulation
    path. normalize_fields then drops any unknown/stale keys."""
    raw = db.run_with_db_retry(
        lambda: db.get_preference(conn, PROMPT_FIELDS_PREF)
    )
    parsed = None
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, list):
                parsed = loaded
        except (json.JSONDecodeError, ValueError):
            parsed = None  # malformed -> fall back to default
    return interpret.normalize_fields(parsed)


class InterpretRequest(BaseModel):
    run_date: str
    scope: str
    model: str
    temperature: float
    seed: int | None = None
    think: bool | None = None
    fields: list[str] | None = None


@app.post("/api/interpret")
def api_interpret(
    body: InterpretRequest,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Synthesize the interpretation for one lane (run_date, scope) with the chosen
    model + parameters, persisting the canonical row and logging the invocation.
    An empty lane returns {skipped: true} with nothing written. Any provider-seam
    failure (missing key, out-of-range temperature, unknown provider) is a clean
    400 with the message, never a raw 500. Keys never leave the server."""
    _lane_to_bucket(body.scope)  # 422 on a bad lane
    allowed = _allowed_models(conn)
    if body.model not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"model must be one of {allowed}",
        )
    # Normalize + persist the field selection (remember last-used, mirroring how
    # model/temperature/seed persist). Done before generation so the choice is
    # saved even if the provider call later errors.
    fields = interpret.normalize_fields(body.fields)

    def _save_pref():
        with db.transaction(conn):
            db.set_preference(conn, PROMPT_FIELDS_PREF,
                              json.dumps(fields), config.now_local_iso())

    db.run_with_db_retry(_save_pref)

    try:
        result = interpret.synthesize_lane(
            conn, body.run_date, body.scope, body.model,
            temperature=body.temperature, seed=body.seed, think=body.think,
            fields=fields,
        )
    except llm.LLMError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # On the written path, echo the model so the client renders the "Generated by"
    # meta line immediately; a skipped result carries no model (nothing was
    # generated, so the UI shows the empty/skip state, not a card).
    if not result.get("skipped"):
        result["model"] = body.model
    return result


@app.get("/api/interpret-defaults")
def api_interpret_defaults(conn: sqlite3.Connection = Depends(get_conn)):
    """Prepopulation for the generate controls: the model dropdown options, the
    per-model honored-parameter map (so the client greys controls a model ignores),
    and the last-used model/temperature/seed (the MAX(id) invocation), or defaults
    on an empty log. The selected model is clamped into the offered set so a model
    later disabled/deleted never shows as a phantom selection."""
    row = db.run_with_db_retry(lambda: db.fetch_latest_invocation(conn))
    models = db.run_with_db_retry(lambda: _allowed_models(conn))
    # capabilities reads the flags straight from each model's registry row (the
    # source of truth), built ONLY over the offered `models`. fetch_model returns the
    # row regardless of flags, but every model here already cleared the dropdown
    # filter, so the row exists.
    capabilities = {
        m: {"temperature": bool(r["supports_temperature"]),
            "seed": bool(r["supports_seed"]),
            # `reasoning` SHOWS the think toggle (the model can think); `think`
            # ENABLES it (this provider's adapter actually honors the toggle). A
            # reasoning model on a non-honoring provider shows a disabled, off toggle.
            "reasoning": bool(r["is_reasoning"]),
            "think": bool(r["is_reasoning"]) and r["provider"] in llm.THINK_PROVIDERS}
        for m in models
        if (r := db.fetch_model(conn, m)) is not None
    }

    default_model = models[0] if models else None
    if row is None:
        model = default_model
        temperature = interpret.DEFAULT_TEMPERATURE
        seed = None
    else:
        model = row["model"] if row["model"] in models else default_model
        temperature = row["temperature"]
        seed = row["seed"]

    return {
        "models": models,
        "model": model,
        "temperature": temperature,
        "seed": seed,
        "capabilities": capabilities,
        # The selectable prompt fields (options) + the persisted selection. The
        # multi-select renders all options with these pre-checked.
        "available_fields": interpret.PROMPT_FIELD_OPTIONS,
        "selected_fields": _stored_prompt_fields(conn),
    }


def _price_models(rows: list[sqlite3.Row]) -> dict:
    """Turn fetch_spend rows into priced per-model dicts plus the section total. Cost
    is already computed in SQL (per invocation, against its effective model_prices
    window) — this only shapes display fields, the single source of truth so the
    frontend never re-does cost math. A row whose cost is NULL (no window covered any
    of its invocations) is the 'unavailable' path: tokens still report, cost is null,
    it is EXCLUDED from total_cost so the labeled total stays honest. has_unpriced is
    set whenever ANY invocation in the section went unpriced (fetch_spend's
    unpriced_invocations), even if a model was only partially priced.

    Each priced row also carries two derived values:
      share_pct        = cost / total_cost  (this model's slice of the scope spend)
      cost_per_million = cost / (in + out) * 1e6  (blended $/M, the efficiency key)
    Both are null when cost is null; share_pct is also null when total_cost is 0
    and cost_per_million is null when the model logged zero tokens (no divide-by-
    zero). share_pct needs the scope total, so it is filled in a second pass."""
    per_model, total, has_unpriced = [], 0.0, False
    for r in rows:
        cost = r["cost"]  # summed in SQL; None when no window covered the model
        if r["unpriced_invocations"]:
            has_unpriced = True
        if cost is not None:
            total += cost
        tokens = r["input_tokens"] + r["output_tokens"]
        cost_per_million = (
            cost / tokens * 1_000_000 if cost is not None and tokens > 0 else None
        )
        per_model.append({
            "model": r["model"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "invocations": r["invocations"],
            "cost": cost,
            "cost_per_million": cost_per_million,
            "share_pct": None,  # filled below, once the scope total is known
        })
    for m in per_model:
        if m["cost"] is not None and total > 0:
            m["share_pct"] = m["cost"] / total
    return {"total_cost": total, "has_unpriced": has_unpriced, "per_model": per_model}


@app.get("/api/spend")
def api_spend(
    run_date: str | None = Query(default=None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Two LLM-spend breakdowns, each split by model: the selected run, and the
    current Eastern calendar month. The two sections use DELIBERATELY DIFFERENT
    time keys — `run` filters by run_date (the pipeline run summarized);
    `month_to_date` filters by generated_at's Eastern month (when the spend was
    incurred) — so a run re-interpreted today shows under its old run AND under this
    month. Do not collapse them. Tokens are exact (from llm_invocations); dollar cost
    is computed per invocation against its effective model_prices window (in
    fetch_spend, never stored), null for any invocation no price window covers. An
    empty log returns zeroed sections, not an error. run_date is optional: absent ->
    the run section is empty (month-to-date still computes)."""
    month = config.now_local_iso()[:7]
    run_rows = (
        db.run_with_db_retry(lambda: db.fetch_spend(conn, run_date=run_date))
        if run_date else []
    )
    month_rows = db.run_with_db_retry(
        lambda: db.fetch_spend(conn, month_prefix=month)
    )
    return {
        "run_date": run_date,
        "month": month,
        "run": _price_models(run_rows),
        "month_to_date": _price_models(month_rows),
        # Palette/thresholds/radii for the spend visual. Served here so the colors
        # live in settings.toml, never hardcoded in the JS/CSS (single source).
        "viz": config.SPEND_VIZ,
    }


# --- /models registry editor (Phase 2) --------------------------------------
# Edit the models + model_prices tables. Forward-use writes (insert/update/delete)
# HONOR the soft-delete flag; pricing is unaffected by it (spend ignores deleted),
# so soft-delete is non-destructive. Price rows are immutable data: the only writes
# to an existing price row are the soft-delete/restore flip and add-price's
# auto-close. All wrapped in run_with_db_retry + transaction.

def _rows(rows) -> list[dict]:
    return [{k: r[k] for k in r.keys()} for r in rows]


class ModelUpdate(BaseModel):
    enabled: bool
    supports_temperature: bool
    supports_seed: bool
    is_reasoning: bool
    notes: str | None = None
    max_tokens: int | None = None


class ModelInsert(BaseModel):
    model: str
    supports_temperature: bool
    supports_seed: bool
    is_reasoning: bool
    enabled: bool = True
    notes: str | None = None
    max_tokens: int | None = None


def _resolve_max_tokens(provider: str, max_tokens, *, cloud_required: bool):
    """Enforce the per-model output-cap policy and return the value to store.

    A local/free provider must stay uncapped (null); a cloud/paid provider must carry
    a positive int <= config.MAX_TOKENS_UPPER_BOUND. Raises HTTPException(422) with a
    clear, distinct detail on violation. `cloud_required` is True on edit (the editor
    owns the cap, so a cloud model can never be saved uncapped) and False on insert
    (a missing cap there means "use the factory default" — the caller omits it so
    db.insert_model applies config.default_max_tokens). The "required" vs "positive"
    messages are distinct so a cleared cloud field reads honestly, not "must be
    positive"."""
    if provider in config.LOCAL_PROVIDERS:
        if max_tokens is not None:
            raise HTTPException(
                status_code=422,
                detail=f"max_tokens must be null for local provider {provider!r}")
        return None
    if max_tokens is None:
        if cloud_required:
            raise HTTPException(
                status_code=422,
                detail=f"max_tokens is required for {provider!r} "
                       "(a paid model must be capped)")
        return None
    if max_tokens <= 0:
        raise HTTPException(status_code=422,
                            detail="max_tokens must be a positive integer")
    if max_tokens > config.MAX_TOKENS_UPPER_BOUND:
        raise HTTPException(
            status_code=422,
            detail=f"max_tokens must be <= {config.MAX_TOKENS_UPPER_BOUND}")
    return max_tokens


class PriceInsert(BaseModel):
    valid_from: str
    input_per_1m: float
    output_per_1m: float


@app.get("/api/models")
def api_list_models(
    include_deleted: bool = Query(default=False),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Every model row for the editor (optionally including soft-deleted). UNLIKE
    the generate dropdown this does not require a current price. Also returns the
    supported-provider list so the add-model picker has ONE source of truth (the
    backend constant), never a hardcoded JS list that drifts."""
    rows = db.run_with_db_retry(
        lambda: db.fetch_models(conn, include_deleted=include_deleted)
    )
    # The max_tokens block is the ONE backend source for the editor's cap defaults,
    # ceiling, and local-provider set, so the JS hardcodes none of those numbers.
    return {
        "models": _rows(rows),
        "providers": list(llm.SUPPORTED_PROVIDERS),
        "max_tokens": {
            "default": config.DEFAULT_MAX_TOKENS,
            "default_reasoning": config.DEFAULT_MAX_TOKENS_REASONING,
            "upper_bound": config.MAX_TOKENS_UPPER_BOUND,
            "local_providers": sorted(config.LOCAL_PROVIDERS),
        },
    }


@app.put("/api/models/{model}")
def api_update_model(
    model: str, body: ModelUpdate,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Edit one model's flags + notes + output cap. PK-locked: `model` comes only from
    the path and is never written, so a `model` in the body is ignored (the doc's PK
    edit rejection). The editor owns the cap, so it is always written (cloud->value,
    local->null) and cloud_required=True forbids saving a paid model uncapped."""
    try:
        provider, _ = llm.split_model(model)
    except llm.LLMError as e:
        raise HTTPException(status_code=422, detail=str(e))
    cap = _resolve_max_tokens(provider, body.max_tokens, cloud_required=True)

    def write():
        with db.transaction(conn):
            return db.update_model(
                conn, model=model, enabled=body.enabled,
                supports_temperature=body.supports_temperature,
                supports_seed=body.supports_seed,
                is_reasoning=body.is_reasoning, notes=body.notes,
                max_tokens=cap)
    if db.run_with_db_retry(write) == 0:
        raise HTTPException(status_code=404, detail=f"no model {model!r}")
    return {"model": model, **body.model_dump(), "max_tokens": cap}


@app.post("/api/models")
def api_insert_model(
    body: ModelInsert, conn: sqlite3.Connection = Depends(get_conn),
):
    """Add a model. The provider is DERIVED from the canonical string via the one
    splitter (never trusted from the client) and must be supported. A duplicate PK
    is a 409 (insert-if-absent never overwrites)."""
    if not body.model.strip():
        raise HTTPException(status_code=422, detail="model must be non-empty")
    try:
        provider, _ = llm.split_model(body.model)
    except llm.LLMError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if provider not in llm.SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail=f"provider must be one of {list(llm.SUPPORTED_PROVIDERS)}")
    # Validate the cap (cloud_required=False: an omitted cap is allowed and means
    # "use the factory default"). When omitted, do NOT pass max_tokens so insert_model's
    # _UNSET path applies config.default_max_tokens — a cloud model auto-gets its
    # reasoning-aware default (never uncapped), a local gets null.
    _resolve_max_tokens(provider, body.max_tokens, cloud_required=False)
    now = config.now_local_iso()
    extra = {} if body.max_tokens is None else {"max_tokens": body.max_tokens}

    def write():
        with db.transaction(conn):
            return db.insert_model(
                conn, model=body.model, provider=provider, enabled=body.enabled,
                supports_temperature=body.supports_temperature,
                supports_seed=body.supports_seed,
                is_reasoning=body.is_reasoning, notes=body.notes, now=now, **extra)
    if db.run_with_db_retry(write) == 0:
        raise HTTPException(status_code=409,
                            detail=f"model {body.model!r} already exists")
    # Echo the EFFECTIVE stored cap (the same factory default insert_model applied when
    # omitted), so the client never shows a misleading null for a defaulted cloud model.
    effective = (body.max_tokens if body.max_tokens is not None
                 else config.default_max_tokens(provider, body.is_reasoning))
    return {"model": body.model, "provider": provider,
            **body.model_dump(exclude={"model"}), "max_tokens": effective}


@app.post("/api/models/{model}/delete")
def api_delete_model(model: str, conn: sqlite3.Connection = Depends(get_conn)):
    """Soft-delete a model (hides it from the dropdown; spend still prices history)."""
    def write():
        with db.transaction(conn):
            return db.set_model_deleted(conn, model=model, deleted=True)
    if db.run_with_db_retry(write) == 0:
        raise HTTPException(status_code=404, detail=f"no model {model!r}")
    return {"model": model, "deleted": True}


@app.post("/api/models/{model}/restore")
def api_restore_model(model: str, conn: sqlite3.Connection = Depends(get_conn)):
    def write():
        with db.transaction(conn):
            return db.set_model_deleted(conn, model=model, deleted=False)
    if db.run_with_db_retry(write) == 0:
        raise HTTPException(status_code=404, detail=f"no model {model!r}")
    return {"model": model, "deleted": False}


@app.get("/api/models/{model}/prices")
def api_list_prices(
    model: str, include_deleted: bool = Query(default=False),
    conn: sqlite3.Connection = Depends(get_conn),
):
    rows = db.run_with_db_retry(
        lambda: db.fetch_prices(conn, model, include_deleted=include_deleted)
    )
    return {"model": model, "prices": _rows(rows)}


@app.post("/api/models/{model}/prices")
def api_insert_price(
    model: str, body: PriceInsert,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Add a price window: auto-close the prior open window(s) and insert a new open
    one, in one transaction. Validation fail-closed: well-formed date, non-negative
    prices, and valid_from strictly after the latest existing window's start (across
    ALL windows incl. soft-deleted — spend ignores deleted, so the non-overlap
    invariant must hold over deleted windows too, or the spend JOIN double-counts)."""
    try:
        date.fromisoformat(body.valid_from)
    except ValueError:
        raise HTTPException(status_code=422,
                            detail="valid_from must be a YYYY-MM-DD date")
    if body.input_per_1m < 0 or body.output_per_1m < 0:
        raise HTTPException(status_code=422, detail="prices must be non-negative")
    if db.fetch_model(conn, model) is None:
        raise HTTPException(status_code=404, detail=f"no model {model!r}")
    now = config.now_local_iso()

    def write():
        with db.transaction(conn):
            latest = db.latest_price_window(conn, model)
            if latest is not None and body.valid_from <= latest["valid_from"]:
                raise HTTPException(
                    status_code=422,
                    detail="valid_from must be after the latest price window's "
                           f"start ({latest['valid_from']})")
            prior_id = (latest["id"]
                        if latest is not None and latest["valid_to"] is None
                        else None)
            new_id = db.insert_price_window(
                conn, model=model, input_per_1m=body.input_per_1m,
                output_per_1m=body.output_per_1m, valid_from=body.valid_from, now=now)
            return new_id, prior_id
    new_id, prior_id = db.run_with_db_retry(write)
    return {"id": new_id, "model": model, "valid_from": body.valid_from,
            "input_per_1m": body.input_per_1m, "output_per_1m": body.output_per_1m,
            "auto_closed": prior_id}


@app.post("/api/prices/{price_id}/delete")
def api_delete_price(price_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    """Soft-delete a price window. Spend ignores the flag, so this only affects the
    dropdown's current-price check and the editor view."""
    def write():
        with db.transaction(conn):
            return db.set_price_deleted(conn, price_id=price_id, deleted=True)
    if db.run_with_db_retry(write) == 0:
        raise HTTPException(status_code=404,
                            detail=f"no price window {price_id}")
    return {"id": price_id, "deleted": True}


@app.post("/api/prices/{price_id}/restore")
def api_restore_price(price_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    def write():
        with db.transaction(conn):
            return db.set_price_deleted(conn, price_id=price_id, deleted=False)
    if db.run_with_db_retry(write) == 0:
        raise HTTPException(status_code=404,
                            detail=f"no price window {price_id}")
    return {"id": price_id, "deleted": False}


@app.get("/api/models/{model}/invocation-count")
def api_model_invocation_count(
    model: str, conn: sqlite3.Connection = Depends(get_conn),
):
    """The delete-warning count for a model: how many logged runs reference it."""
    count = db.run_with_db_retry(
        lambda: db.count_invocations_for_model(conn, model))
    return {"model": model, "count": count}


@app.get("/api/prices/{price_id}/invocation-count")
def api_price_invocation_count(
    price_id: int, conn: sqlite3.Connection = Depends(get_conn),
):
    """The delete-warning count for a price window: invocations whose Eastern date
    falls in it (the shared half-open predicate, so it matches what spend prices)."""
    row = conn.execute(
        "SELECT model, valid_from, valid_to FROM model_prices WHERE id = ?",
        (price_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no price window {price_id}")
    count = db.count_invocations_in_window(
        conn, row["model"], row["valid_from"], row["valid_to"])
    return {"id": price_id, "model": row["model"], "valid_from": row["valid_from"],
            "valid_to": row["valid_to"], "count": count}


# --- Price-refresh agent (Phase 3): prices/deltas, review, model, run -------
#
# A tracked amendment to the read-only-strict dashboard contract (see
# docs/dashboard-plan.MD), mirroring the interpret-write carve-out: one read, two
# proposal writes, get/set the active extraction model, and one run trigger.

@app.get("/api/price-refresh")
def api_price_refresh(conn: sqlite3.Connection = Depends(get_conn)):
    """Current prices + day-over-day deltas per priced model, and the pending (>=10%)
    proposals awaiting a human. Read-only; empty -> empty lists, never 404."""
    prices = db.run_with_db_retry(lambda: store.fetch_price_overview(conn))
    proposals = db.run_with_db_retry(
        lambda: _rows(store.fetch_pending_proposals(conn)))
    return {"prices": prices, "proposals": proposals}


@app.post("/api/price-proposals/{proposal_id}/confirm")
def api_confirm_proposal(
    proposal_id: int, conn: sqlite3.Connection = Depends(get_conn),
):
    """Apply a pending proposal (opens/supersedes a window, carrying the other field
    forward) and mark it confirmed. PriceRefreshError (no active window) -> clean 422;
    unknown/not-pending -> 404."""
    try:
        ok = db.run_with_db_retry(
            lambda: store.confirm_proposal(conn, proposal_id))
    except store.PriceRefreshError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404,
                            detail=f"no pending proposal {proposal_id}")
    return {"id": proposal_id, "status": "confirmed"}


@app.post("/api/price-proposals/{proposal_id}/reject")
def api_reject_proposal(
    proposal_id: int, conn: sqlite3.Connection = Depends(get_conn),
):
    """Mark a pending proposal rejected; opens no window. Unknown/not-pending -> 404."""
    ok = db.run_with_db_retry(lambda: store.reject_proposal(conn, proposal_id))
    if not ok:
        raise HTTPException(status_code=404,
                            detail=f"no pending proposal {proposal_id}")
    return {"id": proposal_id, "status": "rejected"}


@app.get("/api/extraction-model")
def api_get_extraction_model(conn: sqlite3.Connection = Depends(get_conn)):
    """The persisted active extraction model (the shared cron+dashboard read path),
    plus the capable options for the dropdown. `options` is _allowed_models (capable
    priced registry models incl. ollama) — the engine that READS pages, NOT the
    non-local priced set shown in the prices panel."""
    model = db.run_with_db_retry(lambda: daily.active_extraction_model(conn))
    options = db.run_with_db_retry(lambda: _allowed_models(conn))
    return {"model": model, "options": options}


class ExtractionModelUpdate(BaseModel):
    model: str


@app.post("/api/extraction-model")
def api_set_extraction_model(
    body: ExtractionModelUpdate,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Persist the active extraction model. Validated against _allowed_models before
    upsert — an unknown/incapable model is a clean 422 and nothing is written."""
    if body.model not in _allowed_models(conn):
        raise HTTPException(
            status_code=422, detail=f"unknown or unusable model {body.model!r}")
    now = config.now_local_iso()

    def write():
        with db.transaction(conn):
            db.set_preference(conn, daily.ACTIVE_MODEL_PREF, body.model, now)

    db.run_with_db_retry(write)
    return {"model": body.model}


@app.post("/api/price-refresh/run")
def api_price_refresh_run(conn: sqlite3.Connection = Depends(get_conn)):
    """Trigger one run of the daily agent using the PERSISTED active model. Returns the
    run summary + a server-measured duration_ms for the stopwatch. A partial/per-source
    failure is absorbed into summary.errors -> 200 with counts; only a whole-run SETUP
    failure (bad/incapable model, invalid config) is a clean 400, never a raw 500. No
    temp/seed param exists, so a click can't invalidate the determinism contract."""
    start = time.monotonic()
    try:
        summary = daily.run_daily(conn)
    except (ValueError, config.ConfigError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    duration_ms = int((time.monotonic() - start) * 1000)
    return {"applied": summary.applied, "staged": summary.staged,
            "rejected": summary.rejected, "skipped": summary.skipped,
            "errors": summary.errors, "duration_ms": duration_ms}


# --- Documentation page: filesystem-discovered docs (read-only) -------------
#
# Registered BEFORE the static mounts / catch-all so these /api paths keep their
# specificity. The server only scans the tree and serves bytes; all document parsing
# is client-side, so no parsing dependency enters this path. Not /api/docs (that is
# the OpenAPI UI, docs_url above).

# Media type per format, so a direct hit on the raw URL is labeled correctly. md/html
# are fetched inline by the client; no Content-Disposition is set, so navigating to a
# raw URL renders rather than force-downloads (the attachment disposition returns with
# the Phase 2/3 download fallback).
_DOC_MEDIA_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


def get_publish_root() -> str:
    """Absolute publish root. Honors DASHBOARD_PUBLISH_PATH (used to point a smoke or
    test at a throwaway tree); otherwise resolves config.PUBLISH_ROOT against the repo
    root so it is independent of the process CWD. Mirrors get_db_path. Tests override
    this via app.dependency_overrides."""
    override = os.environ.get("DASHBOARD_PUBLISH_PATH")
    base = override if override else str(_REPO_ROOT / config.PUBLISH_ROOT)
    return os.path.realpath(base)


@app.get("/api/documentation")
def get_documentation(root: str = Depends(get_publish_root)):
    """The freshly scanned registry: tabs in display order, each with its documents.
    relPath is an internal serving detail and is stripped from the response."""
    tabs = discovery.scan_publish(root)
    return {"tabs": [
        {"tabId": t["tabId"], "tabLabel": t["tabLabel"], "order": t["order"],
         "docs": [{"docId": d["docId"], "title": d["title"],
                   "format": d["format"], "mode": d["mode"]} for d in t["docs"]]}
        for t in tabs
    ]}


@app.get("/api/documentation/raw")
def get_documentation_raw(tab: str, doc: str,
                          root: str = Depends(get_publish_root)):
    """Serve one document's bytes. The (tab, doc) pair must match an entry in the
    freshly scanned registry (a dynamic whitelist), and the resolved file must stay
    inside the publish root (rejecting path traversal and symlink escape). The path
    served comes from the matched entry, never built from the raw params."""
    entry = next(
        (d for t in discovery.scan_publish(root) if t["tabId"] == tab
         for d in t["docs"] if d["docId"] == doc),
        None,
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="unknown document")

    root_real = os.path.realpath(root)
    file_real = os.path.realpath(os.path.join(root_real, entry["relPath"]))
    if os.path.commonpath([root_real, file_real]) != root_real or not os.path.isfile(file_real):
        # The entry's real path escapes the root (symlink) or vanished between scan
        # and serve. Refuse rather than read outside the published tree.
        raise HTTPException(status_code=404, detail="unknown document")

    return FileResponse(file_real, media_type=_DOC_MEDIA_TYPES.get(entry["format"]))


# Static assets + SPA shell. Registered AFTER every /api route above.
#
# Asset roots are mounted per directory (js/css/vendor are the only ones under
# static/), NOT as a single mount at "/". A Mount("/") compiles to /{path:path}
# and matches EVERY request, so it would swallow client routes like /board and
# /trends (StaticFiles 404s a missing path; html=True does not fall back to
# index.html for non-directory paths), and any catch-all after it would be dead
# code. Per-directory mounts only claim their own prefix, leaving page paths to
# the catch-all below.
app.mount("/js", StaticFiles(directory=str(_STATIC_DIR / "js")), name="js")
app.mount("/css", StaticFiles(directory=str(_STATIC_DIR / "css")), name="css")
app.mount("/vendor", StaticFiles(directory=str(_STATIC_DIR / "vendor")), name="vendor")


@app.get("/{path:path}", include_in_schema=False)
async def spa(path: str):
    """Serve the app shell for any non-/api, non-asset path so deep links and
    refreshes on client routes (/, /board, /trends, future pages) return index.html
    instead of a 404; the vanilla router then renders the matching page. The /api
    routes above match first by specificity; this guard keeps an unmatched /api/*
    a 404 rather than serving HTML in its place (path has no leading slash here)."""
    if path.startswith("api"):
        raise HTTPException(status_code=404)
    return FileResponse(str(_STATIC_DIR / "index.html"))
