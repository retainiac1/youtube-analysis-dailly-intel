import db

EXPECTED_TABLES = {
    "videos",
    "channels",
    "stats_snapshots",
    "rankings",
    "run_log",
    "quota_ledger",
    "categories",
    "interpretations",
    "llm_invocations",
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
    "categories": {"category_id", "title", "region_code", "last_updated_at"},
    "interpretations": {"run_date", "scope", "text", "model", "generated_at"},
    "llm_invocations": {
        "id", "run_date", "scope", "model", "temperature", "seed", "filter",
        "input_tokens", "output_tokens", "generated_at",
    },
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
        assert version == db.SCHEMA_VERSION == 4
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


def test_init_db_creates_llm_invocations(tmp_path):
    """Fresh DB: llm_invocations exists with the param columns at version 4."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        cols = _columns(conn, "llm_invocations")
        assert {"temperature", "seed", "filter"}.issubset(cols)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 4
    finally:
        conn.close()


def _build_v3_db(db_path):
    """Construct a real v3 schema (every current statement except the new
    llm_invocations table), stamped user_version = 3, with a sample videos row
    and a sample interpretations row. Returns the two seeded rows as dicts."""
    conn = db.get_connection(db_path)
    try:
        for statement in db.SCHEMA_STATEMENTS:
            if "llm_invocations" in statement:
                continue
            conn.execute(statement)
        conn.execute("PRAGMA user_version = 3")
        conn.execute(
            "INSERT INTO videos (video_id, title, first_seen_at, last_api_refresh_at) "
            "VALUES ('vid1', 'Sample', '2026-06-08T10:00:00-04:00', "
            "'2026-06-08T10:00:00-04:00')"
        )
        conn.execute(
            "INSERT INTO interpretations (run_date, scope, text, model, generated_at) "
            "VALUES ('2026-06-08', 'overall', 'A summary.', "
            "'anthropic:claude-haiku-4-5', '2026-06-08T11:00:00-04:00')"
        )
        conn.commit()
        video = dict(conn.execute("SELECT * FROM videos").fetchone())
        interp = dict(conn.execute("SELECT * FROM interpretations").fetchone())
    finally:
        conn.close()
    return video, interp


def test_v3_to_v4_migration_is_non_destructive(tmp_path):
    """A v3 DB gains llm_invocations and bumps to v4 with pre-existing rows
    byte-for-byte unchanged."""
    db_path = str(tmp_path / "test.db")
    video_before, interp_before = _build_v3_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert "llm_invocations" not in _table_names(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        conn.close()

    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert "llm_invocations" in _table_names(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        video_after = dict(conn.execute("SELECT * FROM videos").fetchone())
        interp_after = dict(conn.execute("SELECT * FROM interpretations").fetchone())
        assert video_after == video_before
        assert interp_after == interp_before
    finally:
        conn.close()


def test_upsert_interpretation_inserts_and_overwrites(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            db.upsert_interpretation(
                conn, "2026-06-08", "overall", "First.",
                "anthropic:claude-haiku-4-5", "2026-06-08T11:00:00-04:00",
            )
            # A different scope must be untouched by the overwrite below.
            db.upsert_interpretation(
                conn, "2026-06-08", "health", "Health summary.",
                "anthropic:claude-haiku-4-5", "2026-06-08T11:00:00-04:00",
            )
        with db.transaction(conn):
            db.upsert_interpretation(
                conn, "2026-06-08", "overall", "Second.",
                "openai:gpt-5.4-nano", "2026-06-08T12:00:00-04:00",
            )

        rows = conn.execute(
            "SELECT scope, text, model, generated_at FROM interpretations "
            "ORDER BY scope"
        ).fetchall()
        assert len(rows) == 2
        overall = next(r for r in rows if r["scope"] == "overall")
        assert overall["text"] == "Second."
        assert overall["model"] == "openai:gpt-5.4-nano"
        assert overall["generated_at"] == "2026-06-08T12:00:00-04:00"
        health = next(r for r in rows if r["scope"] == "health")
        assert health["text"] == "Health summary."
    finally:
        conn.close()


def test_log_invocation_appends_and_round_trips(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            db.log_invocation(
                conn, "2026-06-08", "overall", "openai:gpt-5.4-nano",
                0.7, 42, None, 3000, 200, "2026-06-08T11:00:00-04:00",
            )
            # Same (run_date, scope), NULL seed and NULL filter, appends a 2nd row.
            db.log_invocation(
                conn, "2026-06-08", "overall", "anthropic:claude-haiku-4-5",
                0.5, None, None, 2500, 150, "2026-06-08T11:05:00-04:00",
            )

        rows = conn.execute(
            "SELECT id, temperature, seed, filter, input_tokens, output_tokens "
            "FROM llm_invocations ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["id"] != rows[1]["id"]  # distinct ids: it is a log
        assert rows[0]["temperature"] == 0.7
        assert rows[0]["seed"] == 42
        assert rows[0]["filter"] is None
        assert rows[0]["input_tokens"] == 3000
        assert rows[0]["output_tokens"] == 200
        assert rows[1]["seed"] is None
        assert rows[1]["filter"] is None
    finally:
        conn.close()
