import sqlite3

import db


def _table_names(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row["name"] for row in rows}


# --- Schema bump (v2 -> v3) -------------------------------------------------

def test_fresh_db_creates_interpretations_and_stamps_v3(tmp_path):
    db_path = str(tmp_path / "fresh.db")
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert "interpretations" in _table_names(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == db.SCHEMA_VERSION == 3
    finally:
        conn.close()


def test_seeded_v2_db_migrates_without_harming_seed(tmp_path):
    """Load-bearing migration proof: a pre-existing v2 DB with a seed videos row
    gains the interpretations table and the v3 stamp, and the seed row is left
    byte-for-byte unchanged."""
    db_path = str(tmp_path / "seeded.db")

    # Build a v2-era DB by hand: the videos table (subset of columns is fine for
    # this proof), a known seed row, user_version stamped to 2, NO interpretations.
    raw = sqlite3.connect(db_path)
    raw.row_factory = sqlite3.Row
    try:
        raw.execute(
            """
            CREATE TABLE videos (
                video_id TEXT PRIMARY KEY,
                title TEXT,
                view_count INTEGER,
                user_notes TEXT DEFAULT '',
                starred INTEGER DEFAULT 0
            )
            """
        )
        raw.execute(
            "INSERT INTO videos (video_id, title, view_count, user_notes, starred) "
            "VALUES ('seed', 'Seed video', 4242, 'hand-written note', 1)"
        )
        raw.execute("PRAGMA user_version = 2")
        raw.commit()
        before = dict(
            raw.execute("SELECT * FROM videos WHERE video_id = 'seed'").fetchone()
        )
    finally:
        raw.close()

    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert "interpretations" in _table_names(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 3
        after = dict(
            conn.execute("SELECT * FROM videos WHERE video_id = 'seed'").fetchone()
        )
        assert after == before
    finally:
        conn.close()


# --- Read helpers -----------------------------------------------------------

def _seed_lane(conn):
    conn.execute(
        "INSERT INTO videos (video_id, title, channel_title, link, thumbnail_url, "
        "view_count, views_to_subs_ratio) VALUES "
        "('vid1', 'First', 'Chan A', 'http://yt/vid1', 'http://thumb/1', 1234, 9.5)"
    )
    conn.execute(
        "INSERT INTO rankings (run_date, bucket, rank, video_id, metric_value, "
        "captured_at) VALUES "
        "('2026-06-08', 'health', 1, 'vid1', 9.5, '2026-06-08T10:00:00-04:00')"
    )
    # A ranking whose video parent is missing (no videos row for 'ghost').
    conn.execute(
        "INSERT INTO rankings (run_date, bucket, rank, video_id, metric_value, "
        "captured_at) VALUES "
        "('2026-06-08', 'health', 2, 'ghost', 3.1, '2026-06-08T10:00:00-04:00')"
    )
    # An earlier run_date so descending order is observable.
    conn.execute(
        "INSERT INTO rankings (run_date, bucket, rank, video_id, metric_value, "
        "captured_at) VALUES "
        "('2026-06-07', 'health', 1, 'vid1', 8.0, '2026-06-07T10:00:00-04:00')"
    )
    conn.commit()


def test_fetch_run_dates_descending(tmp_path):
    conn = db.get_connection(str(tmp_path / "t.db"))
    try:
        db.init_db(str(tmp_path / "t.db"))
        conn.close()
        conn = db.get_connection(str(tmp_path / "t.db"))
        _seed_lane(conn)
        dates = [r["run_date"] for r in db.fetch_run_dates(conn)]
        assert dates == ["2026-06-08", "2026-06-07"]
    finally:
        conn.close()


def test_fetch_lane_joins_and_tolerates_missing_parent(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        _seed_lane(conn)
        rows = db.fetch_lane(conn, "2026-06-08", "health")
        assert [r["rank"] for r in rows] == [1, 2]
        # rank 1: joined to its video.
        assert rows[0]["title"] == "First"
        assert rows[0]["link"] == "http://yt/vid1"
        assert rows[0]["view_count"] == 1234
        # rank 2: missing video parent -> NULL video fields, row still present.
        assert rows[1]["title"] is None
        assert rows[1]["metric_value"] == 3.1
    finally:
        conn.close()


def test_fetch_interpretation_present_and_absent(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO interpretations (run_date, scope, text, model, "
            "generated_at) VALUES ('2026-06-08', 'health', 'Looks strong.', "
            "'model-x', '2026-06-08T10:05:00-04:00')"
        )
        conn.commit()
        row = db.fetch_interpretation(conn, "2026-06-08", "health")
        assert row is not None
        assert row["text"] == "Looks strong."
        assert row["model"] == "model-x"
        assert db.fetch_interpretation(conn, "2026-06-08", "habit") is None
    finally:
        conn.close()


# --- Read-path lock retry ---------------------------------------------------

def test_read_survives_db_locked():
    """The exact wrapper the dashboard endpoints use degrades a transient lock to
    a retry, not an error. Mirrors test_persist.py's write-path retry test."""
    calls = {"n": 0}

    def query():
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return ["ok"]

    assert db.run_with_db_retry(query, sleep=lambda *_: None) == ["ok"]
    assert calls["n"] == 2
