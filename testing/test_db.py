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
        "input_tokens", "output_tokens", "generated_at", "duration_ms",
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
        assert version == db.SCHEMA_VERSION == 5
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
    """Fresh DB: llm_invocations exists with the param + duration columns at v5."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        cols = _columns(conn, "llm_invocations")
        assert {"temperature", "seed", "filter", "duration_ms"}.issubset(cols)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 5
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


def test_v3_to_v5_migration_is_non_destructive(tmp_path):
    """A v3 DB gains llm_invocations (with duration_ms) and bumps straight to v5
    with pre-existing rows byte-for-byte unchanged."""
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
        assert "duration_ms" in _columns(conn, "llm_invocations")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        video_after = dict(conn.execute("SELECT * FROM videos").fetchone())
        interp_after = dict(conn.execute("SELECT * FROM interpretations").fetchone())
        assert video_after == video_before
        assert interp_after == interp_before
    finally:
        conn.close()


# The v4 llm_invocations CREATE, BEFORE duration_ms (nine columns). Used to build a
# genuine v4 DB so the v4->v5 ADD COLUMN path is actually exercised — building it
# from the CURRENT statement (which already has duration_ms) would test nothing.
_V4_LLM_INVOCATIONS = """
CREATE TABLE llm_invocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT,
    scope TEXT,
    model TEXT,
    temperature REAL,
    seed INTEGER,
    filter TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    generated_at TEXT
)
"""


def _build_v4_db(db_path):
    """A real v4 schema: every current CREATE EXCEPT the new llm_invocations (use
    the pre-duration_ms statement instead), stamped user_version = 4, with a sample
    interpretations row and a sample llm_invocations row (no duration_ms). Returns
    the two seeded rows as dicts."""
    conn = db.get_connection(db_path)
    try:
        for statement in db.SCHEMA_STATEMENTS:
            if "llm_invocations" in statement:
                continue
            conn.execute(statement)
        conn.execute(_V4_LLM_INVOCATIONS)
        conn.execute("PRAGMA user_version = 4")
        conn.execute(
            "INSERT INTO interpretations (run_date, scope, text, model, generated_at) "
            "VALUES ('2026-06-08', 'overall', 'A summary.', "
            "'anthropic:claude-haiku-4-5', '2026-06-08T11:00:00-04:00')"
        )
        conn.execute(
            "INSERT INTO llm_invocations (run_date, scope, model, temperature, seed, "
            "filter, input_tokens, output_tokens, generated_at) VALUES "
            "('2026-06-08', 'overall', 'anthropic:claude-haiku-4-5', 0.5, NULL, "
            "NULL, 100, 30, '2026-06-08T11:00:00-04:00')"
        )
        conn.commit()
        interp = dict(conn.execute("SELECT * FROM interpretations").fetchone())
        invocation = dict(conn.execute("SELECT * FROM llm_invocations").fetchone())
    finally:
        conn.close()
    return interp, invocation


def test_v4_to_v5_adds_duration_column_non_destructively(tmp_path):
    """An existing v4 DB whose llm_invocations LACKS duration_ms gains the column
    (ALTER), bumps to v5, and leaves pre-existing rows unchanged (NULL duration)."""
    db_path = str(tmp_path / "test.db")
    interp_before, invocation_before = _build_v4_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert "duration_ms" not in _columns(conn, "llm_invocations")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        conn.close()

    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert "duration_ms" in _columns(conn, "llm_invocations")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        interp_after = dict(conn.execute("SELECT * FROM interpretations").fetchone())
        assert interp_after == interp_before
        inv_after = dict(conn.execute("SELECT * FROM llm_invocations").fetchone())
        # Pre-existing invocation is intact; the new column reads NULL for it.
        assert inv_after["duration_ms"] is None
        assert {k: inv_after[k] for k in invocation_before} == invocation_before
    finally:
        conn.close()


def test_v4_to_v5_migration_is_atomic(tmp_path):
    """The ALTER and the user_version stamp are one atomic unit under an explicit
    BEGIN: a rollback after both must leave version 4 AND no duration_ms column.
    If the PRAGMA committed independently, the version would read 5 — this proves
    it participates in the transaction."""
    db_path = str(tmp_path / "test.db")
    _build_v4_db(db_path)

    conn = db.get_connection(db_path)
    try:
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE llm_invocations ADD COLUMN duration_ms INTEGER")
        conn.execute("PRAGMA user_version = 5")
        conn.execute("ROLLBACK")

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert "duration_ms" not in _columns(conn, "llm_invocations")
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
                duration_ms=1234,
            )
            # Same (run_date, scope), NULL seed/filter, default (NULL) duration_ms,
            # appends a 2nd row.
            db.log_invocation(
                conn, "2026-06-08", "overall", "anthropic:claude-haiku-4-5",
                0.5, None, None, 2500, 150, "2026-06-08T11:05:00-04:00",
            )

        rows = conn.execute(
            "SELECT id, temperature, seed, filter, input_tokens, output_tokens, "
            "duration_ms FROM llm_invocations ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["id"] != rows[1]["id"]  # distinct ids: it is a log
        assert rows[0]["temperature"] == 0.7
        assert rows[0]["seed"] == 42
        assert rows[0]["filter"] is None
        assert rows[0]["input_tokens"] == 3000
        assert rows[0]["output_tokens"] == 200
        assert rows[0]["duration_ms"] == 1234
        assert rows[1]["seed"] is None
        assert rows[1]["filter"] is None
        assert rows[1]["duration_ms"] is None  # keyword-only default
    finally:
        conn.close()


def test_fetch_interpretation_returns_latest_duration(tmp_path):
    """fetch_interpretation reports the duration of the NEWEST invocation for the
    (run_date, scope) — the run behind the current text — and NULL when none."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            db.upsert_interpretation(
                conn, "2026-06-08", "overall", "A summary.",
                "openai:gpt-5.4-nano", "2026-06-08T11:05:00-04:00",
            )
            # Two invocations for the same lane; the later (higher id) is the one
            # whose duration must be reported alongside the current text.
            db.log_invocation(
                conn, "2026-06-08", "overall", "openai:gpt-5.4-nano",
                0.7, 42, None, 3000, 200, "2026-06-08T11:00:00-04:00",
                duration_ms=900,
            )
            db.log_invocation(
                conn, "2026-06-08", "overall", "openai:gpt-5.4-nano",
                0.7, 42, None, 3100, 210, "2026-06-08T11:05:00-04:00",
                duration_ms=1700,
            )

        row = db.fetch_interpretation(conn, "2026-06-08", "overall")
        assert row["text"] == "A summary."
        assert row["duration_ms"] == 1700  # newest invocation's duration

        # A lane with an interpretation but no invocation -> NULL duration.
        with db.transaction(conn):
            db.upsert_interpretation(
                conn, "2026-06-08", "health", "Seed text.",
                "model-x", "2026-06-08T11:05:00-04:00",
            )
        seed_row = db.fetch_interpretation(conn, "2026-06-08", "health")
        assert seed_row["text"] == "Seed text."
        assert seed_row["duration_ms"] is None
    finally:
        conn.close()


def test_fetch_latest_invocation_empty_returns_none(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert db.fetch_latest_invocation(conn) is None
    finally:
        conn.close()


def test_fetch_latest_invocation_returns_max_id_row(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            db.log_invocation(
                conn, "2026-06-08", "overall", "openai:gpt-5.4-nano",
                0.7, 42, None, 3000, 200, "2026-06-08T11:00:00-04:00",
            )
            # The later insert (higher id) is the one prepopulation should return.
            db.log_invocation(
                conn, "2026-06-08", "health", "anthropic:claude-haiku-4-5",
                0.3, None, None, 2500, 150, "2026-06-08T11:05:00-04:00",
            )

        row = db.fetch_latest_invocation(conn)
        assert row["model"] == "anthropic:claude-haiku-4-5"
        assert row["temperature"] == 0.3
        assert row["seed"] is None
    finally:
        conn.close()
