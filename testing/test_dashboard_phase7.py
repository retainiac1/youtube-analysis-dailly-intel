"""Phase 7 dashboard surface: the run-state / run-summary endpoints and the
result-frame summary attachment."""
import sqlite3

import pytest

import config
import db


def _seed(db_path):
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        rid = db.start_run(conn, "discover", config.now_local_iso())
        db.finish_run(conn, rid, config.now_local_iso(), 0, 0, "success")
        with db.transaction(conn):
            db.write_run_summary(
                conn, rid, "2026-06-25", "discover",
                {"added": 1, "updated": 2, "aged_out": 0, "gone": 0}, 0.05, 10,
                "2026-06-25T10:00:00-04:00")
            db.write_run_summary(
                conn, rid + 1, "2026-06-25", "refresh",
                {"added": 0, "updated": 3, "aged_out": 0, "gone": 0}, 0.0, 12,
                "2026-06-25T12:00:00-04:00")
    finally:
        conn.close()
    return rid


def test_run_state_and_run_summary_endpoints(tmp_path):
    from fastapi.testclient import TestClient
    from dashboard import app as appmod
    db_path = str(tmp_path / "dash.db")
    _seed(db_path)
    # Override the DB path dependency (the documented test seam) and do NOT enter the
    # lifespan (no `with`), so this can never run init_db against the live config.DB_PATH.
    appmod.app.dependency_overrides[appmod.get_db_path] = lambda: db_path
    try:
        client = TestClient(appmod.app)
        state = client.get("/api/run-state").json()
        assert state["discover_ran_today"] is True       # the seeded today discover

        summaries = client.get("/api/run-summary?limit=1").json()["summaries"]
        assert len(summaries) == 1                        # respects limit
        assert summaries[0]["mode"] == "refresh"          # newest first (higher run_id)
    finally:
        appmod.app.dependency_overrides.clear()


def test_lifespan_refuses_to_auto_migrate_existing_old_db(tmp_path, monkeypatch):
    """Startup must REFUSE to auto-migrate an EXISTING behind-version DB (the
    auto-migrate footgun: a --reload edit to db.py would otherwise silently migrate
    the live seed and bypass the backup gate). A fresh/missing path is created and
    starts cleanly; a current DB is a no-op."""
    from fastapi.testclient import TestClient
    from dashboard import app as appmod

    # An existing DB stamped behind SCHEMA_VERSION: the lifespan must fail loud.
    old = str(tmp_path / "old.db")
    raw = sqlite3.connect(old)
    raw.execute("CREATE TABLE videos (video_id TEXT PRIMARY KEY)")
    raw.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION - 1}")
    raw.commit()
    raw.close()
    monkeypatch.setenv("DASHBOARD_DB_PATH", old)
    with pytest.raises(RuntimeError, match="Refusing to start"):
        with TestClient(appmod.app):
            pass
    # Refusal does not migrate: the old DB is left at its prior version.
    assert sqlite3.connect(old).execute(
        "PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION - 1

    # A missing path: created + stamped current, starts cleanly.
    fresh = str(tmp_path / "fresh.db")
    monkeypatch.setenv("DASHBOARD_DB_PATH", fresh)
    with TestClient(appmod.app):
        pass
    assert db.migration_pending(fresh) is False

    # A current DB: no-op, starts cleanly.
    with TestClient(appmod.app):
        pass


def test_result_event_attaches_summary_or_omits():
    from dashboard import extract
    with_summary = extract.result_event(
        0, "ok", summary={"mode": "discover", "added": 1})
    assert '"summary"' in with_summary and '"discover"' in with_summary
    # A dry run passes summary=None -> no summary key in the frame.
    without = extract.result_event(0, "ok", summary=None)
    assert '"summary"' not in without
