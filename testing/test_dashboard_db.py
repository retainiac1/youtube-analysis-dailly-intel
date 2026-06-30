import sqlite3

import db


def _table_names(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row["name"] for row in rows}


# --- Schema bump (interpretations present, current stamp) -------------------

def test_fresh_db_creates_interpretations_and_stamps_current(tmp_path):
    db_path = str(tmp_path / "fresh.db")
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert "interpretations" in _table_names(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == db.SCHEMA_VERSION == 13
    finally:
        conn.close()


def test_seeded_v2_db_migrates_without_harming_seed(tmp_path):
    """Load-bearing migration proof: a pre-existing v2 DB with a seed videos row
    gains the interpretations table and the current stamp, and the seed row is
    left byte-for-byte unchanged. The IF NOT EXISTS + user_version path carries a
    v2 DB forward to the current version in one init_db."""
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
                starred INTEGER DEFAULT 0,
                first_seen_at TEXT
            )
            """
        )
        raw.execute(
            "INSERT INTO videos (video_id, title, view_count, user_notes, starred, "
            "first_seen_at) VALUES ('seed', 'Seed video', 4242, 'hand-written note', "
            "1, '2026-06-08T10:00:00-04:00')"
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
        assert version == db.SCHEMA_VERSION == 13
        after = dict(
            conn.execute("SELECT * FROM videos WHERE video_id = 'seed'").fetchone()
        )
        # The pre-existing columns are byte-for-byte unchanged; v12 adds the three
        # snapshot-sweep columns on top (status backfilled active, growth clock
        # falling back to first_seen_at since this seed has no snapshots).
        assert {k: after[k] for k in before} == before
        assert after["status"] == "active"
        assert after["status_changed_at"] is None
        assert after["last_view_growth_at"] == "2026-06-08T10:00:00-04:00"
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


def _seed_lifecycle(conn):
    """A lane whose depth lives in stats_snapshots, not the board. vidA is tracked
    four days (06-05 to 06-08) but on the board only twice (06-07, 06-08): the proof
    that curves come from the full snapshot history, not on-board runs. vidB has a
    single snapshot (no measurable velocity/growth, zero lifespan)."""
    conn.execute(
        "INSERT INTO videos (video_id, title, channel_title, link, published_at, "
        "view_count) VALUES "
        "('vidA', 'Alpha', 'Chan A', 'http://yt/vidA', "
        "'2026-06-01T09:00:00-04:00', 10000)"
    )
    conn.execute(
        "INSERT INTO videos (video_id, title, channel_title, link, published_at, "
        "view_count) VALUES "
        "('vidB', 'Beta', 'Chan B', 'http://yt/vidB', "
        "'2026-06-02T09:00:00-04:00', 8000)"
    )
    # rankings = lane membership only (vidA on two runs, vidB on one).
    for rd in ("2026-06-07", "2026-06-08"):
        conn.execute(
            "INSERT INTO rankings (run_date, bucket, rank, video_id, metric_value, "
            f"captured_at) VALUES ('{rd}', 'health', 1, 'vidA', 9.0, "
            f"'{rd}T10:00:00-04:00')"
        )
    conn.execute(
        "INSERT INTO rankings (run_date, bucket, rank, video_id, metric_value, "
        "captured_at) VALUES ('2026-06-08', 'health', 2, 'vidB', 7.0, "
        "'2026-06-08T10:00:00-04:00')"
    )
    # stats_snapshots = full tracked history (vidA's four days exceed its two board
    # runs); UNIQUE(run_id, video_id).
    snaps = [
        (1, "vidA", "2026-06-05T10:00:00-04:00", 1000, 100, 10),
        (2, "vidA", "2026-06-06T10:00:00-04:00", 3000, 200, 20),
        (3, "vidA", "2026-06-07T10:00:00-04:00", 6000, 300, 30),
        (4, "vidA", "2026-06-08T10:00:00-04:00", 10000, 400, 40),
        (4, "vidB", "2026-06-08T10:00:00-04:00", 8000, 800, 40),
    ]
    for rid, vid, cap, vc, lc, cc in snaps:
        conn.execute(
            "INSERT INTO stats_snapshots (run_id, video_id, captured_at, "
            f"view_count, like_count, comment_count) VALUES ({rid}, '{vid}', "
            f"'{cap}', {vc}, {lc}, {cc})"
        )
    conn.commit()


def test_fetch_dashboard_lifecycle_is_snapshot_driven(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        _seed_lifecycle(conn)
        data = db.fetch_dashboard_lifecycle(conn, "health")

        # Pinned top-level shape: no survivorship/engagement wrappers, no cut keys.
        assert set(data) == {
            "run_count", "summary", "growth", "ratio_series", "maturation",
            "rank_history", "lifespan_distribution",
        }
        assert "survivorship" not in data and "engagement" not in data
        assert "churn" not in data and "runs_ranked" not in data

        # run_count survives, and is the rankings runs in window.
        assert data["run_count"] == 2

        # Depth from snapshots, NOT the board: vidA's curve is its full four-day
        # history though it was ranked only twice, and carries a clickable link.
        ga = next(s for s in data["growth"]["series"] if s["video_id"] == "vidA")
        assert len(ga["points"]) == 4
        assert ga["link"] == "http://yt/vidA"
        # The growth cohort is selected by PEAK views/day and carries a velocity
        # series (the Views-per-day fall-off curve replots it). vidA's peak is its
        # fastest day-over-day segment (06-07 to 06-08 = 4000/day).
        assert ga["velocity"]
        assert max(p["views_per_day"] for p in ga["velocity"]) == 4000

        # Summary is snapshot behavior, not board trivia.
        s = data["summary"]
        assert set(s) == {
            "median_tracked_lifespan_days", "median_peak_velocity",
            "median_total_view_growth",
        }
        # lifespan: vidA spans 3 days (06-05 to 06-08), vidB one snapshot = 0 days.
        assert s["median_tracked_lifespan_days"] == 1.5
        # peak velocity: vidA's fastest day-over-day is 06-07 to 06-08 = 4000/day;
        # vidB (one snapshot) contributes none.
        assert s["median_peak_velocity"] == 4000
        # total growth: vidA 10000 - 1000 = 9000; vidB excluded (needs >= 2 snaps).
        assert s["median_total_view_growth"] == 9000

        # Lifespan distribution covers every population video with snapshots.
        assert sum(b["count"] for b in data["lifespan_distribution"]) == 2

        # Maturation tooltips can name + link the video.
        assert data["maturation"]
        assert all("video_id" in m and "link" in m for m in data["maturation"])
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
