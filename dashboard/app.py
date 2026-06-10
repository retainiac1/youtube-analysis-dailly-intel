"""Read API for the daily-intel dashboard.

Read-only over swipefile.db: JSON endpoints (leaderboard, filters, quota,
interpretation, and the Phase 2 change-over-time charts) plus the static frontend.
Imports only db.py and config.py (both stdlib-only); never swipefile.py. Every DB
read goes through db.run_with_db_retry so a pipeline write-lock degrades to a
short retry rather than a 500.
"""

import os
import sqlite3
import sys
from pathlib import Path

# Repo root holds config.py and db.py. Put it on sys.path so this app imports them
# whether launched via `uvicorn dashboard.app:app` or as a package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi import Depends, FastAPI, HTTPException, Query  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# The three leaderboard lanes. health/habit are rankings.bucket values and also
# videos.buckets members; overall is the whole-pool lane (a rankings.bucket
# value, not a videos.buckets member). The lane maps directly to rankings.bucket.
VALID_LANES = {"health", "habit", "overall"}

app = FastAPI(title="daily-intel dashboard", docs_url="/api/docs")


def _lane_to_bucket(lane: str) -> str:
    """Validate a lane query param and return the rankings.bucket it maps to
    (identity). A bad value is a 422, mirroring FastAPI's own param validation."""
    if lane not in VALID_LANES:
        raise HTTPException(
            status_code=422,
            detail=f"lane must be one of {sorted(VALID_LANES)}",
        )
    return lane


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
    """Stored interpretation text for (run_date, scope). Absent text renders as a
    clean empty state (status 200, empty string), never an error."""
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
        }
    return {
        "run_date": run_date,
        "scope": scope,
        "text": row["text"],
        "model": row["model"],
        "generated_at": row["generated_at"],
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


# Static page. Mounted LAST so it does not shadow the /api routes.
app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
