import sqlite3
import threading

import config
import db


def _query_in_thread(conn):
    """Run a trivial query on `conn` from a fresh thread; return the exception
    raised there (or None). sqlite3 raises ProgrammingError when a connection is
    used off its creating thread and check_same_thread is True."""
    box = {}

    def run():
        try:
            conn.execute("SELECT 1").fetchone()
            box["err"] = None
        except Exception as e:  # noqa: BLE001 - we want to inspect it
            box["err"] = e

    t = threading.Thread(target=run)
    t.start()
    t.join()
    return box["err"]


def test_get_connection_default_blocks_cross_thread(tmp_path):
    """The pipeline contract: the default connection is single-thread guarded."""
    path = str(tmp_path / "t.db")
    db.init_db(path)
    conn = db.get_connection(path)
    try:
        assert isinstance(_query_in_thread(conn), sqlite3.ProgrammingError)
    finally:
        conn.close()


def test_get_connection_check_same_thread_false_allows_cross_thread(tmp_path):
    """The dashboard opens connections with check_same_thread=False so FastAPI's
    threadpool can create the connection on one worker and use it on another
    (concurrent chart requests) without a ProgrammingError."""
    path = str(tmp_path / "t.db")
    db.init_db(path)
    conn = db.get_connection(path, check_same_thread=False)
    try:
        assert _query_in_thread(conn) is None
    finally:
        conn.close()


def test_runs_lists_run_dates(client):
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    assert resp.json() == {"run_dates": ["2026-06-08"]}


def test_rankings_returns_lane(client):
    resp = client.get("/api/rankings", params={"run_date": "2026-06-08", "bucket": "health"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_date"] == "2026-06-08"
    assert body["bucket"] == "health"
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["rank"] == 1
    assert row["title"] == "First video"
    assert row["link"] == "http://yt/vid1"
    assert row["view_count"] == 1234


def test_rankings_empty_lane_is_empty_list(client):
    resp = client.get("/api/rankings", params={"run_date": "2026-06-08", "bucket": "habit"})
    assert resp.status_code == 200
    assert resp.json()["rows"] == []


def test_rankings_missing_param_is_422(client):
    assert client.get("/api/rankings", params={"run_date": "2026-06-08"}).status_code == 422
    assert client.get("/api/rankings", params={"bucket": "health"}).status_code == 422


def test_quota_reports_used_and_cap(client):
    resp = client.get("/api/quota")
    assert resp.status_code == 200
    body = resp.json()
    cap = config.DAILY_QUOTA_LIMIT - config.SAFETY_BUFFER
    assert body["pacific_date"] == config.pacific_date()
    assert body["units_used"] == 4200
    assert body["cap"] == cap
    assert body["remaining"] == cap - 4200


def test_interpretation_present(client):
    resp = client.get("/api/interpretation", params={"run_date": "2026-06-08", "scope": "health"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "Looks strong."
    assert body["model"] == "model-x"


def test_interpretation_absent_is_empty_text(client):
    resp = client.get("/api/interpretation", params={"run_date": "2026-06-08", "scope": "habit"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == ""
    assert body["model"] is None


def test_interpretation_full_shape(client):
    """Populated row returns every field the Interpretation page renders."""
    resp = client.get(
        "/api/interpretation", params={"run_date": "2026-06-08", "scope": "health"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_date"] == "2026-06-08"
    assert body["scope"] == "health"
    assert body["text"] == "Looks strong."
    assert body["model"] == "model-x"
    assert body["generated_at"] == "2026-06-08T10:05:00-04:00"


def test_interpretation_absent_scope_is_clean_empty(client):
    """A scope with no row (overall) returns the clean empty contract, not an
    error: 200 with empty text and null metadata."""
    resp = client.get(
        "/api/interpretation", params={"run_date": "2026-06-08", "scope": "overall"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == ""
    assert body["model"] is None
    assert body["generated_at"] is None


def test_interpretation_scope_keys_the_lookup(client):
    """Same run, different scope: health has a row, habit does not. The page sends
    scope=<active lane>, so the populated panel only appears for the lane with a
    row; this guards that scope (not just run_date) selects the interpretation."""
    health = client.get(
        "/api/interpretation", params={"run_date": "2026-06-08", "scope": "health"}).json()
    habit = client.get(
        "/api/interpretation", params={"run_date": "2026-06-08", "scope": "habit"}).json()
    assert health["text"] == "Looks strong."
    assert habit["text"] == ""


def test_placeholder_page_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "daily-intel dashboard" in resp.text


def test_index_asset_links_resolve(client):
    """Every CSS/JS the page references must actually serve. Guards against a
    wrong path (the static mount is at '/', so links must NOT carry a '/static'
    prefix)."""
    import re

    page = client.get("/").text
    assets = re.findall(r'(?:href|src)="(/[^"]+\.(?:css|js))"', page)
    assert assets, "index.html references no local css/js"
    for url in assets:
        assert client.get(url).status_code == 200, f"asset 404: {url}"
