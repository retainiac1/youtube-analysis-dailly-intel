import db

EXPECTED_TABLES = {
    "videos",
    "channels",
    "stats_snapshots",
    "rankings",
    "run_log",
    "quota_ledger",
}

EXPECTED_COLUMNS = {
    "videos": {
        "video_id", "title", "channel_id", "channel_title", "published_at",
        "duration_seconds", "is_short", "link", "thumbnail_url", "description",
        "category_id", "audio_language", "definition", "has_captions",
        "made_for_kids", "tags", "topic_categories", "top_comments",
        "matched_queries", "buckets", "view_count", "like_count", "comment_count",
        "views_to_subs_ratio", "views_per_day", "first_seen_at",
        "last_api_refresh_at", "user_notes", "starred", "starred_at", "hook",
        "first_10_sec", "saves",
    },
    "channels": {
        "channel_id", "subscriber_count", "channel_video_count",
        "channel_total_views", "channel_created_date", "channel_country",
        "channel_keywords", "last_updated_at",
    },
    "stats_snapshots": {
        "id", "run_id", "video_id", "captured_at", "view_count", "like_count",
        "comment_count",
    },
    "rankings": {
        "run_date", "bucket", "rank", "video_id", "metric_value", "captured_at",
    },
    "run_log": {
        "run_id", "mode", "started_at", "finished_at", "quota_used",
        "videos_seen", "status",
    },
    "quota_ledger": {"pacific_date", "units_used", "updated_at"},
}


def _table_names(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row["name"] for row in rows}


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def test_init_db_creates_all_tables(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        # Subset, not equality: AUTOINCREMENT also creates sqlite_sequence.
        assert EXPECTED_TABLES.issubset(_table_names(conn))
    finally:
        conn.close()


def test_init_db_table_columns(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        for table, expected in EXPECTED_COLUMNS.items():
            assert _columns(conn, table) == expected, table
    finally:
        conn.close()


def test_init_db_is_idempotent(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    # Running a second time must not raise and must leave tables intact.
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert EXPECTED_TABLES.issubset(_table_names(conn))
    finally:
        conn.close()


def test_user_version_is_set(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == db.SCHEMA_VERSION == 1
    finally:
        conn.close()


def test_stats_snapshots_unique_run_video(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO stats_snapshots (run_id, video_id, captured_at) "
            "VALUES (1, 'vid', '2026-06-07')"
        )
        # Same (run_id, video_id) must violate the UNIQUE guard.
        import sqlite3
        import pytest

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO stats_snapshots (run_id, video_id, captured_at) "
                "VALUES (1, 'vid', '2026-06-07')"
            )
    finally:
        conn.close()
