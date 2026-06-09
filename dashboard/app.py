"""Phase 0 read API for the daily-intel dashboard.

Read-only over swipefile.db: four JSON endpoints plus a placeholder static page.
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

from fastapi import Depends, FastAPI, Query  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402

_STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="daily-intel dashboard", docs_url="/api/docs")


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
    FastAPI runs the post-yield finally after the response is sent."""
    conn = db.get_connection(db_path)
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


# Static placeholder page. Mounted LAST so it does not shadow the /api routes.
app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
