import pytest

import config
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
    "app_preferences",
    "models",
    "model_prices",
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
    "interpretations": {
        "run_date", "scope", "text", "model", "generated_at", "temperature",
        "seed", "think",
    },
    "llm_invocations": {
        "id", "run_date", "scope", "model", "temperature", "seed", "filter",
        "input_tokens", "output_tokens", "generated_at", "duration_ms",
    },
    "app_preferences": {"key", "value", "updated_at"},
    "models": {
        "model", "provider", "enabled", "supports_temperature", "supports_seed",
        "is_reasoning", "max_tokens", "deleted", "added_at", "notes",
    },
    "model_prices": {
        "id", "model", "input_per_1m", "output_per_1m", "valid_from", "valid_to",
        "deleted", "recorded_at",
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
        assert version == db.SCHEMA_VERSION == 9
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
    """Fresh DB: llm_invocations exists with the param + duration columns at the
    current schema version."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        cols = _columns(conn, "llm_invocations")
        assert {"temperature", "seed", "filter", "duration_ms"}.issubset(cols)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == db.SCHEMA_VERSION
    finally:
        conn.close()


def test_init_db_seeds_baseline_models(tmp_path):
    """Fresh DB: init_db offline-seeds the baseline models (enabled, not deleted)
    each with one open price window, with the capability flags captured as data —
    so the app is never broken on a freshly-migrated DB."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        models = {
            r["model"]: r
            for r in conn.execute("SELECT * FROM models").fetchall()
        }
        assert set(models) == {
            "anthropic:claude-haiku-4-5", "openai:gpt-5.4-nano",
            "xai:grok-4-fast", "google:gemini-2.5-flash-lite",
            "ollama:qwen3.5:9b",
        }
        # Capability flags as data: anthropic omits seed; the gpt-5 reasoning model
        # omits temperature and is flagged reasoning; the rest honor both.
        anthropic = models["anthropic:claude-haiku-4-5"]
        assert anthropic["supports_temperature"] == 1
        assert anthropic["supports_seed"] == 0
        assert anthropic["is_reasoning"] == 0
        assert anthropic["enabled"] == 1 and anthropic["deleted"] == 0
        nano = models["openai:gpt-5.4-nano"]
        assert nano["supports_temperature"] == 0
        assert nano["supports_seed"] == 1
        assert nano["is_reasoning"] == 1
        grok = models["xai:grok-4-fast"]
        assert grok["supports_temperature"] == 1 and grok["supports_seed"] == 1
        # The local Ollama thinking model: temperature + seed honored, is_reasoning.
        ollama = models["ollama:qwen3.5:9b"]
        assert ollama["provider"] == "ollama"
        assert ollama["supports_temperature"] == 1 and ollama["supports_seed"] == 1
        assert ollama["is_reasoning"] == 1
        assert ollama["enabled"] == 1 and ollama["deleted"] == 0

        # One open (valid_to IS NULL) price window per model, priced from the seed
        # literal. On an empty log valid_from is today.
        prices = {
            r["model"]: r
            for r in conn.execute(
                "SELECT * FROM model_prices WHERE valid_to IS NULL"
            ).fetchall()
        }
        assert set(prices) == set(models)
        assert prices["anthropic:claude-haiku-4-5"]["input_per_1m"] == 1.00
        assert prices["anthropic:claude-haiku-4-5"]["output_per_1m"] == 5.00
        assert prices["openai:gpt-5.4-nano"]["input_per_1m"] == 0.20
        # Local inference is free: a $0/$0 open window.
        assert prices["ollama:qwen3.5:9b"]["input_per_1m"] == 0.0
        assert prices["ollama:qwen3.5:9b"]["output_per_1m"] == 0.0
    finally:
        conn.close()


def test_init_db_seed_is_insert_if_empty(tmp_path):
    """The offline seed never clobbers operator edits: a model row edited after the
    first init (e.g. soft-deleted) survives a second init_db unchanged, and no
    duplicate price window is added."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            conn.execute(
                "UPDATE models SET deleted = 1, enabled = 0, notes = 'retired' "
                "WHERE model = 'xai:grok-4-fast'"
            )
        before = conn.execute(
            "SELECT count(*) c FROM model_prices WHERE model = 'xai:grok-4-fast'"
        ).fetchone()["c"]
    finally:
        conn.close()

    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT deleted, enabled, notes FROM models WHERE model = 'xai:grok-4-fast'"
        ).fetchone()
        assert row["deleted"] == 1 and row["enabled"] == 0
        assert row["notes"] == "retired"  # operator edit preserved, not re-seeded
        after = conn.execute(
            "SELECT count(*) c FROM model_prices WHERE model = 'xai:grok-4-fast'"
        ).fetchone()["c"]
        assert after == before  # no duplicate window
    finally:
        conn.close()


def _build_v3_db(db_path):
    """Construct a real v3 schema (every current statement except the post-v3
    tables: llm_invocations [v4] and app_preferences [v6]), stamped
    user_version = 3, with a sample videos row and a sample interpretations row.
    Returns the two seeded rows as dicts."""
    conn = db.get_connection(db_path)
    try:
        for statement in db.SCHEMA_STATEMENTS:
            if ("llm_invocations" in statement or "app_preferences" in statement
                    or "models" in statement or "model_prices" in statement):
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


def test_v3_to_current_migration_is_non_destructive(tmp_path):
    """A v3 DB gains llm_invocations (with duration_ms), app_preferences, and the
    v7 models/model_prices tables, and bumps straight to the current schema with
    pre-existing rows byte-for-byte unchanged."""
    db_path = str(tmp_path / "test.db")
    video_before, interp_before = _build_v3_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert "llm_invocations" not in _table_names(conn)
        assert "app_preferences" not in _table_names(conn)
        assert "models" not in _table_names(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        conn.close()

    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert "llm_invocations" in _table_names(conn)
        assert "duration_ms" in _columns(conn, "llm_invocations")
        assert "app_preferences" in _table_names(conn)
        assert {"models", "model_prices"}.issubset(_table_names(conn))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        # Baseline models seeded by the migration.
        assert (conn.execute("SELECT count(*) c FROM models").fetchone()["c"]
                == len(config.SEED_MODELS))
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
            # Skip the current llm_invocations (use the pre-duration_ms statement),
            # app_preferences (a v6 table), and models/model_prices (v7 tables) —
            # none existed at v4.
            if ("llm_invocations" in statement or "app_preferences" in statement
                    or "models" in statement or "model_prices" in statement):
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


def test_v4_to_current_adds_duration_column_non_destructively(tmp_path):
    """An existing v4 DB whose llm_invocations LACKS duration_ms gains the column
    (ALTER), app_preferences, and the v7 models/model_prices tables, bumps to the
    current schema, and leaves pre-existing rows unchanged (NULL duration)."""
    db_path = str(tmp_path / "test.db")
    interp_before, invocation_before = _build_v4_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert "duration_ms" not in _columns(conn, "llm_invocations")
        assert "app_preferences" not in _table_names(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        conn.close()

    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert "duration_ms" in _columns(conn, "llm_invocations")
        assert "app_preferences" in _table_names(conn)
        assert {"models", "model_prices"}.issubset(_table_names(conn))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
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


# The interpretations CREATE BEFORE temperature/seed (five columns). Used to build
# a genuine v6 DB so the v6->v7 ADD COLUMN path on interpretations is actually
# exercised — building it from the CURRENT statement (which already has the two
# columns) would test nothing.
_PRE_V7_INTERPRETATIONS = """
CREATE TABLE interpretations (
    run_date TEXT,
    scope TEXT,
    text TEXT,
    model TEXT,
    generated_at TEXT,
    PRIMARY KEY (run_date, scope)
)
"""


def _build_v6_db(db_path):
    """A real v6 schema: every current CREATE EXCEPT the v7 models/model_prices
    tables, with interpretations at its pre-v7 (no temperature/seed) shape, stamped
    user_version = 6, with a sample interpretations row. Returns the row as a dict."""
    conn = db.get_connection(db_path)
    try:
        for statement in db.SCHEMA_STATEMENTS:
            if "models" in statement or "model_prices" in statement:
                continue
            if "interpretations" in statement:
                statement = _PRE_V7_INTERPRETATIONS
            conn.execute(statement)
        conn.execute("PRAGMA user_version = 6")
        conn.execute(
            "INSERT INTO interpretations (run_date, scope, text, model, generated_at) "
            "VALUES ('2026-06-08', 'overall', 'A summary.', "
            "'anthropic:claude-haiku-4-5', '2026-06-08T11:00:00-04:00')"
        )
        conn.commit()
        interp = dict(conn.execute("SELECT * FROM interpretations").fetchone())
    finally:
        conn.close()
    return interp


def test_v6_to_current_adds_interpretation_columns_non_destructively(tmp_path):
    """A genuine v6 DB (interpretations without temperature/seed, no models table)
    gains the interpretation columns via ALTER, the seeded models/model_prices
    tables, and bumps to the current schema, with the pre-existing interpretation
    preserved."""
    db_path = str(tmp_path / "test.db")
    interp_before = _build_v6_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert "temperature" not in _columns(conn, "interpretations")
        assert "seed" not in _columns(conn, "interpretations")
        assert "models" not in _table_names(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    finally:
        conn.close()

    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert {"temperature", "seed", "think"}.issubset(
            _columns(conn, "interpretations"))
        assert {"models", "model_prices"}.issubset(_table_names(conn))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert (conn.execute("SELECT count(*) c FROM models").fetchone()["c"]
                == len(config.SEED_MODELS))
        interp_after = dict(conn.execute("SELECT * FROM interpretations").fetchone())
        # Pre-existing row intact; the new columns read NULL for it.
        assert interp_after["temperature"] is None
        assert interp_after["seed"] is None
        assert interp_after["think"] is None
        assert {k: interp_after[k] for k in interp_before} == interp_before
    finally:
        conn.close()


def test_interrupted_v6_migration_heals(tmp_path):
    """Simulate an ACTUAL partial v6 migration failure: the new tables got created
    (the autocommit CREATE step) but the atomic block rolled back, so user_version is
    still 6, the interpretation columns are absent, and nothing was seeded. The next
    init_db must heal it completely — distinct from a clean idempotent double-run."""
    db_path = str(tmp_path / "test.db")
    _build_v6_db(db_path)

    conn = db.get_connection(db_path)
    try:
        for statement in db.SCHEMA_STATEMENTS:
            if "models" in statement or "model_prices" in statement:
                conn.execute(statement)
        conn.commit()
        # The broken half-migrated state, set up directly.
        assert {"models", "model_prices"}.issubset(_table_names(conn))
        assert conn.execute("SELECT count(*) c FROM models").fetchone()["c"] == 0
        assert "temperature" not in _columns(conn, "interpretations")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    finally:
        conn.close()

    db.init_db(db_path)  # the heal: a re-run completes the partial migration

    conn = db.get_connection(db_path)
    try:
        assert {"temperature", "seed", "think"}.issubset(
            _columns(conn, "interpretations"))
        assert (conn.execute("SELECT count(*) c FROM models").fetchone()["c"]
                == len(config.SEED_MODELS))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        conn.close()


# The interpretations CREATE at v7 (temperature/seed, no think). Used to build a
# genuine v7 DB so the v7->v8 ADD COLUMN path on interpretations.think is actually
# exercised — building it from the CURRENT statement (which already has think) would
# test nothing.
_PRE_V8_INTERPRETATIONS = """
CREATE TABLE interpretations (
    run_date TEXT,
    scope TEXT,
    text TEXT,
    model TEXT,
    generated_at TEXT,
    temperature REAL,
    seed INTEGER,
    PRIMARY KEY (run_date, scope)
)
"""


def _build_v7_db(db_path):
    """A real v7 schema: every current CREATE, with interpretations at its pre-v8
    (no think) shape, the pre-Ollama baseline models seeded, stamped user_version =
    7, with a sample interpretations row carrying applied temperature/seed. The
    Ollama model (added at v8) is removed so the v7->v8 migration is shown to ADD it.
    Returns the interpretation row as a dict."""
    conn = db.get_connection(db_path)
    try:
        for statement in db.SCHEMA_STATEMENTS:
            if "interpretations" in statement:
                statement = _PRE_V8_INTERPRETATIONS
            conn.execute(statement)
        db._seed_models(conn)
        # A genuine v7 DB predates the local Ollama model; drop it so the migration
        # is shown to seed it.
        conn.execute("DELETE FROM models WHERE model = 'ollama:qwen3.5:9b'")
        conn.execute("DELETE FROM model_prices WHERE model = 'ollama:qwen3.5:9b'")
        conn.execute("PRAGMA user_version = 7")
        conn.execute(
            "INSERT INTO interpretations (run_date, scope, text, model, "
            "generated_at, temperature, seed) VALUES "
            "('2026-06-08', 'overall', 'A summary.', 'anthropic:claude-haiku-4-5', "
            "'2026-06-08T11:00:00-04:00', 0.7, 42)"
        )
        conn.commit()
        interp = dict(conn.execute("SELECT * FROM interpretations").fetchone())
    finally:
        conn.close()
    return interp


def test_v7_to_v8_adds_think_and_seeds_ollama_non_destructively(tmp_path):
    """A genuine v7 DB (interpretations without think, the four pre-Ollama models)
    gains interpretations.think via ALTER, seeds the new local Ollama model with a
    $0 open price window, and bumps to v8, with the pre-existing interpretation
    preserved (think reads NULL for it)."""
    db_path = str(tmp_path / "test.db")
    interp_before = _build_v7_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert "think" not in _columns(conn, "interpretations")
        assert conn.execute(
            "SELECT count(*) c FROM models WHERE model = 'ollama:qwen3.5:9b'"
        ).fetchone()["c"] == 0
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    finally:
        conn.close()

    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert "think" in _columns(conn, "interpretations")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        ollama = conn.execute(
            "SELECT * FROM models WHERE model = 'ollama:qwen3.5:9b'"
        ).fetchone()
        assert ollama is not None and ollama["provider"] == "ollama"
        assert ollama["is_reasoning"] == 1
        assert ollama["supports_temperature"] == 1 and ollama["supports_seed"] == 1
        assert ollama["enabled"] == 1 and ollama["deleted"] == 0
        win = conn.execute(
            "SELECT * FROM model_prices WHERE model = 'ollama:qwen3.5:9b' "
            "AND valid_to IS NULL"
        ).fetchone()
        assert win["input_per_1m"] == 0.0 and win["output_per_1m"] == 0.0
        # Pre-existing row intact; the new column reads NULL for it.
        interp_after = dict(conn.execute("SELECT * FROM interpretations").fetchone())
        assert interp_after["think"] is None
        assert {k: interp_after[k] for k in interp_before} == interp_before
    finally:
        conn.close()


# --- v8 -> v9: models.max_tokens (nullable, reasoning-aware backfill) --------

# A genuine v8 models table predates max_tokens; built from this CREATE (not the
# current statement, which already has the column) so the migration is exercised.
_PRE_V9_MODELS = """
CREATE TABLE models (
    model TEXT PRIMARY KEY,
    provider TEXT,
    enabled INTEGER DEFAULT 1,
    supports_temperature INTEGER,
    supports_seed INTEGER,
    is_reasoning INTEGER,
    deleted INTEGER DEFAULT 0,
    added_at TEXT,
    notes TEXT
)
"""


def _build_v8_db(db_path):
    """A real v8 schema: the current CREATEs but `models` at its pre-v9 (no
    max_tokens) shape, stamped user_version = 8, seeded with three rows that carry
    NO max_tokens column — a non-reasoning cloud model, a reasoning cloud model, and
    the local ollama model — so the v8->v9 migration is shown to ADD and backfill
    the column reasoning-aware."""
    conn = db.get_connection(db_path)
    try:
        for statement in db.SCHEMA_STATEMENTS:
            if "CREATE TABLE IF NOT EXISTS models" in statement:
                statement = _PRE_V9_MODELS
            conn.execute(statement)
        conn.executemany(
            "INSERT INTO models (model, provider, enabled, supports_temperature, "
            "supports_seed, is_reasoning, deleted, added_at, notes) "
            "VALUES (?, ?, 1, 1, 1, ?, 0, '2026-01-01T00:00:00-05:00', NULL)",
            [
                ("anthropic:claude-haiku-4-5", "anthropic", 0),
                ("openai:gpt-5.4-nano", "openai", 1),
                ("ollama:qwen3.5:9b", "ollama", 1),
            ],
        )
        conn.execute("PRAGMA user_version = 8")
        conn.commit()
    finally:
        conn.close()


def test_v8_to_v9_adds_max_tokens_reasoning_aware(tmp_path):
    """A genuine v8 DB (models without max_tokens) gains a nullable max_tokens via
    ALTER, backfilled by is_reasoning: non-reasoning cloud -> lean default (no
    behavior change), reasoning cloud -> generous default (the one intended
    512->5000 fix), local -> NULL (uncapped). Bumps to v9."""
    db_path = str(tmp_path / "test.db")
    _build_v8_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert "max_tokens" not in _columns(conn, "models")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    finally:
        conn.close()

    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert "max_tokens" in _columns(conn, "models")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 9

        def mt(model):
            return conn.execute(
                "SELECT max_tokens FROM models WHERE model = ?", (model,)
            ).fetchone()["max_tokens"]

        assert mt("anthropic:claude-haiku-4-5") == config.DEFAULT_MAX_TOKENS
        assert mt("openai:gpt-5.4-nano") == config.DEFAULT_MAX_TOKENS_REASONING
        assert mt("ollama:qwen3.5:9b") is None
    finally:
        conn.close()


def test_v9_migration_fails_closed_on_uncapped_paid_row(tmp_path):
    """A dirty pre-v9 row — a non-local model with a NULL provider — must not slip
    through `NOT IN (local)` (NULL NOT IN (...) is NULL, not TRUE) and survive
    uncapped. The migration raises and rolls back: version stays 8, no column added,
    so a paid model can never silently end up uncapped."""
    db_path = str(tmp_path / "test.db")
    _build_v8_db(db_path)
    conn = db.get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO models (model, provider, enabled, supports_temperature, "
            "supports_seed, is_reasoning, deleted, added_at, notes) "
            "VALUES ('mystery:x', NULL, 1, 1, 1, 0, 0, "
            "'2026-01-01T00:00:00-05:00', NULL)"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="uncapped"):
        db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
        assert "max_tokens" not in _columns(conn, "models")
    finally:
        conn.close()


def test_init_db_seeds_max_tokens_reasoning_aware(tmp_path):
    """A freshly-seeded DB caps each cloud model by its reasoning flag and leaves
    the local model uncapped — fresh seed == the migration backfill."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        def mt(model):
            return conn.execute(
                "SELECT max_tokens FROM models WHERE model = ?", (model,)
            ).fetchone()["max_tokens"]

        assert mt("openai:gpt-5.4-nano") == config.DEFAULT_MAX_TOKENS_REASONING
        assert mt("anthropic:claude-haiku-4-5") == config.DEFAULT_MAX_TOKENS
        assert mt("xai:grok-4-fast") == config.DEFAULT_MAX_TOKENS
        assert mt("google:gemini-2.5-flash-lite") == config.DEFAULT_MAX_TOKENS
        assert mt("ollama:qwen3.5:9b") is None
    finally:
        conn.close()


# --- model registry readers -------------------------------------------------

def test_fetch_model_returns_row_or_none(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        row = db.fetch_model(conn, "openai:gpt-5.4-nano")
        assert row["provider"] == "openai"
        assert row["supports_temperature"] == 0
        assert row["supports_seed"] == 1
        assert db.fetch_model(conn, "nope:model") is None
    finally:
        conn.close()


def test_fetch_model_ignores_enabled_and_deleted(tmp_path):
    """fetch_model is a plain attribute read: a soft-deleted/disabled model is
    still returned (forward-use gating lives in fetch_dropdown_models, not here)."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            conn.execute(
                "UPDATE models SET deleted = 1, enabled = 0 "
                "WHERE model = 'xai:grok-4-fast'"
            )
        row = db.fetch_model(conn, "xai:grok-4-fast")
        assert row is not None
        assert row["deleted"] == 1 and row["enabled"] == 0
    finally:
        conn.close()


def test_fetch_dropdown_models_filters_enabled_deleted_and_priced(tmp_path):
    """The dropdown honors forward-use flags: enabled=1, deleted=0, and a current
    (open, non-deleted) price window. Disabled, soft-deleted, and priceless models
    are excluded."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            # Disable one, soft-delete another.
            conn.execute(
                "UPDATE models SET enabled = 0 WHERE model = 'openai:gpt-5.4-nano'"
            )
            conn.execute(
                "UPDATE models SET deleted = 1 WHERE model = 'xai:grok-4-fast'"
            )
            # A model with attributes but no current price window.
            conn.execute(
                "INSERT INTO models (model, provider, enabled, supports_temperature, "
                "supports_seed, is_reasoning, deleted, added_at, notes) "
                "VALUES ('openai:priceless', 'openai', 1, 1, 1, 0, 0, "
                "'2026-06-11T10:00:00-04:00', NULL)"
            )
        offered = {r["model"] for r in db.fetch_dropdown_models(conn)}
        # ollama:qwen3.5:9b is seeded enabled with a $0 (still current) window, so it
        # is offered alongside the other enabled+priced models.
        assert offered == {
            "anthropic:claude-haiku-4-5", "google:gemini-2.5-flash-lite",
            "ollama:qwen3.5:9b",
        }
        # Stable order.
        models = [r["model"] for r in db.fetch_dropdown_models(conn)]
        assert models == sorted(models)
    finally:
        conn.close()


def test_fetch_dropdown_models_excludes_when_only_window_is_deleted(tmp_path):
    """'Has a current price' means an OPEN, NON-deleted window: a model whose only
    open window is soft-deleted drops out of the dropdown (but stays priceable for
    spend — that discipline is tested separately)."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            conn.execute(
                "UPDATE model_prices SET deleted = 1 "
                "WHERE model = 'google:gemini-2.5-flash-lite' AND valid_to IS NULL"
            )
        offered = {r["model"] for r in db.fetch_dropdown_models(conn)}
        assert "google:gemini-2.5-flash-lite" not in offered
    finally:
        conn.close()


def _seed_two_windows(conn, model="test:m"):
    """Two adjacent non-overlapping windows for one model: Jan-Jun at 1.0/2.0, then
    Jun-open at 3.0/4.0. The Jun-01 boundary belongs to the later (open) window."""
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO model_prices (model, input_per_1m, output_per_1m, "
            "valid_from, valid_to, deleted, recorded_at) VALUES "
            "(?, 1.0, 2.0, '2026-01-01', '2026-06-01', 0, '2026-01-01T00:00:00-05:00')",
            (model,),
        )
        conn.execute(
            "INSERT INTO model_prices (model, input_per_1m, output_per_1m, "
            "valid_from, valid_to, deleted, recorded_at) VALUES "
            "(?, 3.0, 4.0, '2026-06-01', NULL, 0, '2026-06-01T00:00:00-04:00')",
            (model,),
        )


def test_fetch_effective_price_selects_window_by_date(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        _seed_two_windows(conn)
        # Mid first window.
        assert db.fetch_effective_price(conn, "test:m", "2026-03-01")["input_per_1m"] == 1.0
        # Exactly on the shared boundary -> the LATER (open) window (exclusive upper).
        assert db.fetch_effective_price(conn, "test:m", "2026-06-01")["input_per_1m"] == 3.0
        # After the boundary -> still the open window.
        assert db.fetch_effective_price(conn, "test:m", "2026-09-01")["input_per_1m"] == 3.0
        # Before any window -> None.
        assert db.fetch_effective_price(conn, "test:m", "2025-12-31") is None
    finally:
        conn.close()


def test_fetch_effective_price_gap_returns_none(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO model_prices (model, input_per_1m, output_per_1m, "
                "valid_from, valid_to, deleted, recorded_at) VALUES "
                "('test:m', 1.0, 2.0, '2026-01-01', '2026-02-01', 0, 'x')"
            )
            conn.execute(
                "INSERT INTO model_prices (model, input_per_1m, output_per_1m, "
                "valid_from, valid_to, deleted, recorded_at) VALUES "
                "('test:m', 3.0, 4.0, '2026-03-01', NULL, 0, 'x')"
            )
        # In the hole between the two windows.
        assert db.fetch_effective_price(conn, "test:m", "2026-02-15") is None
    finally:
        conn.close()


def test_fetch_effective_price_ignores_deleted(tmp_path):
    """Backward pricing: a soft-deleted window still prices an invocation whose date
    it covers (the spend discipline ignores deleted)."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO model_prices (model, input_per_1m, output_per_1m, "
                "valid_from, valid_to, deleted, recorded_at) VALUES "
                "('test:m', 7.0, 9.0, '2026-01-01', NULL, 1, 'x')"
            )
        row = db.fetch_effective_price(conn, "test:m", "2026-05-01")
        assert row is not None and row["input_per_1m"] == 7.0
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


def test_upsert_interpretation_persists_and_overwrites_temperature_seed(tmp_path):
    """The applied temperature/seed are stored and overwritten on re-run; seed is
    NULL when the provider omitted it (the Anthropic case)."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            db.upsert_interpretation(
                conn, "2026-06-08", "overall", "First.",
                "openai:gpt-5.4-nano", "2026-06-08T11:00:00-04:00",
                temperature=0.7, seed=42,
            )
        row = conn.execute(
            "SELECT temperature, seed FROM interpretations "
            "WHERE run_date='2026-06-08' AND scope='overall'"
        ).fetchone()
        assert row["temperature"] == 0.7 and row["seed"] == 42

        # Re-run the lane with a provider that dropped the seed (seed=None).
        with db.transaction(conn):
            db.upsert_interpretation(
                conn, "2026-06-08", "overall", "Second.",
                "anthropic:claude-haiku-4-5", "2026-06-08T12:00:00-04:00",
                temperature=0.3, seed=None,
            )
        row = conn.execute(
            "SELECT temperature, seed FROM interpretations "
            "WHERE run_date='2026-06-08' AND scope='overall'"
        ).fetchone()
        assert row["temperature"] == 0.3
        assert row["seed"] is None  # overwritten to NULL, the honest 'no seed'
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


def _seed_spend_log(conn):
    """A spread of invocations across two runs, two models, and two Eastern
    months (the YYYY-MM of generated_at): used by the fetch_spend tests."""
    with db.transaction(conn):
        # run 2026-06-08, June: openai twice (proves GROUP BY collapse) + anthropic.
        db.log_invocation(conn, "2026-06-08", "overall", "openai:gpt-5.4-nano",
                          0.7, 42, None, 3000, 200, "2026-06-08T11:00:00-04:00")
        db.log_invocation(conn, "2026-06-08", "health", "openai:gpt-5.4-nano",
                          0.7, 42, None, 1000, 100, "2026-06-08T11:05:00-04:00")
        db.log_invocation(conn, "2026-06-08", "habit", "anthropic:claude-haiku-4-5",
                          0.5, None, None, 2500, 150, "2026-06-08T11:06:00-04:00")
        # run 2026-05-30, May: a different run AND a different month.
        db.log_invocation(conn, "2026-05-30", "overall", "openai:gpt-5.4-nano",
                          0.7, 42, None, 500, 50, "2026-05-30T22:00:00-04:00")


def test_fetch_spend_groups_by_model_and_sums(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        _seed_spend_log(conn)
        rows = db.fetch_spend(conn)  # no filter: every invocation
        by_model = {r["model"]: r for r in rows}
        # openai appears in three invocations (two June + one May): summed + counted.
        assert by_model["openai:gpt-5.4-nano"]["input_tokens"] == 3000 + 1000 + 500
        assert by_model["openai:gpt-5.4-nano"]["output_tokens"] == 200 + 100 + 50
        assert by_model["openai:gpt-5.4-nano"]["invocations"] == 3
        assert by_model["anthropic:claude-haiku-4-5"]["input_tokens"] == 2500
        assert by_model["anthropic:claude-haiku-4-5"]["invocations"] == 1
        # Ordered by model for a stable readout.
        assert [r["model"] for r in rows] == sorted(r["model"] for r in rows)
    finally:
        conn.close()


def test_fetch_spend_run_date_filter(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        _seed_spend_log(conn)
        rows = db.fetch_spend(conn, run_date="2026-06-08")
        by_model = {r["model"]: r for r in rows}
        # Only the June-08 run: openai twice (3000+1000), anthropic once. The May
        # run's openai row is excluded.
        assert by_model["openai:gpt-5.4-nano"]["input_tokens"] == 4000
        assert by_model["openai:gpt-5.4-nano"]["invocations"] == 2
        assert "anthropic:claude-haiku-4-5" in by_model
    finally:
        conn.close()


def test_fetch_spend_month_prefix_filter(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        _seed_spend_log(conn)
        # The substr(generated_at,1,7) Eastern-month key: only May rows.
        rows = db.fetch_spend(conn, month_prefix="2026-05")
        assert len(rows) == 1
        assert rows[0]["model"] == "openai:gpt-5.4-nano"
        assert rows[0]["input_tokens"] == 500
        assert rows[0]["invocations"] == 1
        # And only June rows the other way.
        june = db.fetch_spend(conn, month_prefix="2026-06")
        assert sum(r["invocations"] for r in june) == 3
    finally:
        conn.close()


def test_fetch_spend_coalesces_null_tokens(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        # A NULL-token invocation (e.g. an un-instrumented seed row) must contribute
        # 0, not null the SUM for its model.
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO llm_invocations (run_date, scope, model, "
                "input_tokens, output_tokens, generated_at) "
                "VALUES ('2026-06-08', 'overall', 'openai:gpt-5.4-nano', "
                "NULL, NULL, '2026-06-08T09:00:00-04:00')"
            )
            db.log_invocation(conn, "2026-06-08", "health", "openai:gpt-5.4-nano",
                              0.7, None, None, 1000, 100, "2026-06-08T09:05:00-04:00")
        rows = db.fetch_spend(conn)
        assert len(rows) == 1
        assert rows[0]["input_tokens"] == 1000  # NULL row added 0, not None
        assert rows[0]["output_tokens"] == 100
        assert rows[0]["invocations"] == 2
    finally:
        conn.close()


# --- fetch_spend per-invocation pricing (cost in SQL) -----------------------
# These all use a custom 'test:m' model with EXPLICIT price windows, so the dates
# are deterministic (independent of the offline seed's valid_from). 1M input / 0
# output makes cost == input_per_1m exactly, so the arithmetic is obvious.

def _price_window(conn, model, inp, out, valid_from, valid_to, deleted=0):
    conn.execute(
        "INSERT INTO model_prices (model, input_per_1m, output_per_1m, "
        "valid_from, valid_to, deleted, recorded_at) VALUES (?, ?, ?, ?, ?, ?, 'x')",
        (model, inp, out, valid_from, valid_to, deleted),
    )


def _invocation(conn, model, generated_at, *, run_date="r", input_tokens=1_000_000,
                output_tokens=0):
    conn.execute(
        "INSERT INTO llm_invocations (run_date, scope, model, input_tokens, "
        "output_tokens, generated_at) VALUES (?, 'overall', ?, ?, ?, ?)",
        (run_date, model, input_tokens, output_tokens, generated_at),
    )


def test_fetch_spend_prices_each_invocation_against_its_own_window(tmp_path):
    """Additive across a mid-month price change: two windows in one month, three
    invocations (one in each window + one exactly on the shared boundary), summed by
    pricing EACH against its own window — not a single flat rate. The boundary date
    belongs to the LATER window (exclusive valid_to)."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            _price_window(conn, "test:m", 1.0, 0.0, "2026-06-01", "2026-06-15")
            _price_window(conn, "test:m", 3.0, 0.0, "2026-06-15", None)
            _invocation(conn, "test:m", "2026-06-10T12:00:00-04:00")  # window 1 -> 1.0
            _invocation(conn, "test:m", "2026-06-20T12:00:00-04:00")  # window 2 -> 3.0
            _invocation(conn, "test:m", "2026-06-15T12:00:00-04:00")  # boundary -> 3.0
        rows = db.fetch_spend(conn, month_prefix="2026-06")
        assert len(rows) == 1
        assert rows[0]["invocations"] == 3
        # 1.0 + 3.0 + 3.0 — per-invocation, not 3 * one rate.
        assert rows[0]["cost"] == pytest.approx(7.0)
        assert rows[0]["unpriced_invocations"] == 0
    finally:
        conn.close()


def test_fetch_spend_prices_by_eastern_date_not_utc(tmp_path):
    """Timezone-straddle: an Eastern-evening timestamp whose UTC day is the NEXT day
    must price against its EASTERN-date window. 2026-06-14T23:30-04:00 is 06-15 in
    UTC but 06-14 Eastern, so it falls in the window ending 06-15 (rate 2.0), not the
    one starting 06-15 (rate 9.0). This is the whole reason for substr(...,1,10)."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            _price_window(conn, "test:m", 2.0, 0.0, "2026-01-01", "2026-06-15")
            _price_window(conn, "test:m", 9.0, 0.0, "2026-06-15", None)
            _invocation(conn, "test:m", "2026-06-14T23:30:00-04:00")
        rows = db.fetch_spend(conn)
        assert rows[0]["cost"] == pytest.approx(2.0)  # Eastern 06-14, NOT UTC 06-15
    finally:
        conn.close()


def test_fetch_spend_gap_between_windows_is_unpriced(tmp_path):
    """A date in a hole between two windows is unpriced: tokens report, cost is NULL,
    unpriced_invocations counts it."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            _price_window(conn, "test:m", 1.0, 0.0, "2026-01-01", "2026-02-01")
            _price_window(conn, "test:m", 3.0, 0.0, "2026-03-01", None)
            _invocation(conn, "test:m", "2026-02-15T12:00:00-05:00")  # in the gap
        rows = db.fetch_spend(conn)
        assert rows[0]["input_tokens"] == 1_000_000  # tokens still reported
        assert rows[0]["cost"] is None
        assert rows[0]["unpriced_invocations"] == 1
    finally:
        conn.close()


def test_fetch_spend_overlapping_windows_double_count_canary(tmp_path):
    """CANARY documenting the non-overlap assumption: if two windows overlap, an
    invocation in the overlap matches BOTH, so the JOIN double-counts its tokens AND
    cost (and COUNT(*) too). Phase 2's add-price auto-close prevents overlap; if that
    ever breaks, this is the symptom — spend silently inflates."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            _price_window(conn, "test:m", 1.0, 0.0, "2026-01-01", None)  # open
            _price_window(conn, "test:m", 2.0, 0.0, "2026-05-01", None)  # open, overlaps
            _invocation(conn, "test:m", "2026-06-01T12:00:00-04:00")  # one invocation
        rows = db.fetch_spend(conn)
        # One invocation, but it joined two windows: everything is doubled.
        assert rows[0]["invocations"] == 2
        assert rows[0]["input_tokens"] == 2_000_000
        assert rows[0]["cost"] == pytest.approx(3.0)  # 1.0 + 2.0, double-counted
    finally:
        conn.close()


def test_fetch_spend_prices_against_deleted_window(tmp_path):
    """Backward pricing ignores deleted: a soft-deleted window still prices an
    invocation whose date it covers (so soft delete never loses spend history)."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            _price_window(conn, "test:m", 4.0, 0.0, "2026-01-01", None, deleted=1)
            _invocation(conn, "test:m", "2026-05-01T12:00:00-04:00")
        rows = db.fetch_spend(conn)
        assert rows[0]["cost"] == pytest.approx(4.0)  # deleted window still prices
        assert rows[0]["unpriced_invocations"] == 0
    finally:
        conn.close()


def test_fetch_spend_and_effective_price_agree_across_boundaries(tmp_path):
    """The shared window predicate, two implementations: fetch_effective_price (one
    model+date) and the fetch_spend JOIN must charge the SAME price for the same
    (model, date) across every boundary case — in-window, the exclusive valid_to
    boundary, a gap, and the NULL-valid_to current window."""
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            _price_window(conn, "test:m", 1.0, 0.0, "2026-01-01", "2026-02-01")
            _price_window(conn, "test:m", 3.0, 0.0, "2026-06-01", None)
        dates = [
            "2026-01-15",  # in window 1
            "2026-02-01",  # exclusive valid_to of window 1 -> gap
            "2026-03-15",  # gap between windows
            "2026-06-01",  # valid_from of the open window (boundary)
            "2026-09-01",  # inside the NULL-valid_to current window
        ]
        with db.transaction(conn):
            for d in dates:
                _invocation(conn, "test:m", f"{d}T12:00:00-04:00", run_date=d)
        for d in dates:
            eff = db.fetch_effective_price(conn, "test:m", d)
            # 1M input / 1e6 * rate == rate, so expected cost IS input_per_1m.
            expected = None if eff is None else eff["input_per_1m"]
            row = db.fetch_spend(conn, run_date=d)[0]
            if expected is None:
                assert row["cost"] is None, f"{d}: spend {row['cost']} vs eff None"
            else:
                assert row["cost"] == pytest.approx(expected), (
                    f"{d}: spend {row['cost']} vs eff {expected}"
                )
    finally:
        conn.close()


# --- model registry editor helpers (Phase 2) --------------------------------
# These use a custom 'test:m' model with EXPLICIT windows so dates are
# deterministic (independent of the offline seed's "today" valid_from).

def _mk_model(conn, model="test:m", *, enabled=1, deleted=0):
    conn.execute(
        "INSERT INTO models (model, provider, enabled, supports_temperature, "
        "supports_seed, is_reasoning, deleted, added_at, notes) "
        "VALUES (?, 'x', ?, 1, 1, 0, ?, '2026-01-01T00:00:00-05:00', NULL)",
        (model, enabled, deleted),
    )


def test_fetch_models_include_deleted(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            _mk_model(conn, "test:gone", deleted=1)
        live = {r["model"] for r in db.fetch_models(conn)}
        assert "test:gone" not in live and "anthropic:claude-haiku-4-5" in live
        allm = {r["model"] for r in db.fetch_models(conn, include_deleted=True)}
        assert "test:gone" in allm
    finally:
        conn.close()


def test_insert_model_inserts_and_duplicate_is_noop(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            n = db.insert_model(
                conn, model="xai:new", provider="xai", enabled=True,
                supports_temperature=True, supports_seed=False, is_reasoning=False,
                notes="hi", now="2026-06-12T10:00:00-04:00")
        assert n == 1
        row = db.fetch_model(conn, "xai:new")
        # Booleans stored as explicit 1/0.
        assert row["enabled"] == 1 and row["supports_temperature"] == 1
        assert row["supports_seed"] == 0 and row["is_reasoning"] == 0
        assert row["provider"] == "xai" and row["notes"] == "hi"
        # A duplicate PK is a no-op (rowcount 0), not an overwrite.
        with db.transaction(conn):
            n2 = db.insert_model(
                conn, model="xai:new", provider="xai", enabled=False,
                supports_temperature=False, supports_seed=False,
                is_reasoning=False, notes="clobber?", now="x")
        assert n2 == 0
        assert db.fetch_model(conn, "xai:new")["notes"] == "hi"  # unchanged
    finally:
        conn.close()


def test_update_model_changes_mutable_cols_never_pk(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            n = db.update_model(
                conn, model="openai:gpt-5.4-nano", enabled=False,
                supports_temperature=True, supports_seed=False,
                is_reasoning=False, notes="edited")
        assert n == 1
        row = db.fetch_model(conn, "openai:gpt-5.4-nano")
        assert row["enabled"] == 0 and row["supports_temperature"] == 1
        assert row["supports_seed"] == 0 and row["notes"] == "edited"
        # The PK is unchanged and there is no second row.
        assert db.fetch_model(conn, "openai:gpt-5.4-nano") is not None
        # Unknown model -> rowcount 0.
        with db.transaction(conn):
            assert db.update_model(
                conn, model="nope:x", enabled=True, supports_temperature=True,
                supports_seed=True, is_reasoning=False, notes=None) == 0
    finally:
        conn.close()


def test_insert_model_defaults_max_tokens_reasoning_aware(tmp_path):
    # Omitting max_tokens applies config.default_max_tokens: reasoning cloud ->
    # generous, non-reasoning cloud -> lean, local -> NULL (uncapped).
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            db.insert_model(conn, model="openai:r", provider="openai", enabled=True,
                            supports_temperature=False, supports_seed=True,
                            is_reasoning=True, notes=None,
                            now="2026-06-12T10:00:00-04:00")
            db.insert_model(conn, model="anthropic:c", provider="anthropic",
                            enabled=True, supports_temperature=True,
                            supports_seed=False, is_reasoning=False, notes=None,
                            now="2026-06-12T10:00:00-04:00")
            db.insert_model(conn, model="ollama:loc", provider="ollama",
                            enabled=True, supports_temperature=True,
                            supports_seed=True, is_reasoning=True, notes=None,
                            now="2026-06-12T10:00:00-04:00")
        assert (db.fetch_model(conn, "openai:r")["max_tokens"]
                == config.DEFAULT_MAX_TOKENS_REASONING)
        assert (db.fetch_model(conn, "anthropic:c")["max_tokens"]
                == config.DEFAULT_MAX_TOKENS)
        assert db.fetch_model(conn, "ollama:loc")["max_tokens"] is None
    finally:
        conn.close()


def test_insert_model_explicit_max_tokens_is_stored(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            db.insert_model(conn, model="xai:x", provider="xai", enabled=True,
                            supports_temperature=True, supports_seed=True,
                            is_reasoning=False, notes=None, max_tokens=1234,
                            now="2026-06-12T10:00:00-04:00")
        assert db.fetch_model(conn, "xai:x")["max_tokens"] == 1234
    finally:
        conn.close()


def test_update_model_sets_max_tokens_when_given(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            db.update_model(conn, model="anthropic:claude-haiku-4-5", enabled=True,
                            supports_temperature=True, supports_seed=False,
                            is_reasoning=False, notes=None, max_tokens=2048)
        assert db.fetch_model(conn, "anthropic:claude-haiku-4-5")["max_tokens"] == 2048
    finally:
        conn.close()


def test_update_model_omitting_max_tokens_leaves_it_unchanged(tmp_path):
    # The PUT endpoint stays on its current path until Phase 2; an omitted
    # max_tokens must not silently wipe the stored cap.
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        before = db.fetch_model(conn, "anthropic:claude-haiku-4-5")["max_tokens"]
        assert before == config.DEFAULT_MAX_TOKENS  # sanity: a real cap to preserve
        with db.transaction(conn):
            db.update_model(conn, model="anthropic:claude-haiku-4-5", enabled=False,
                            supports_temperature=True, supports_seed=False,
                            is_reasoning=False, notes="x")
        assert db.fetch_model(conn, "anthropic:claude-haiku-4-5")["max_tokens"] == before
    finally:
        conn.close()


def test_set_model_deleted_hides_from_dropdown_but_still_prices(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            _mk_model(conn, "test:m")
            _price_window(conn, "test:m", 1.0, 0.0, "2026-01-01", None)
            _invocation(conn, "test:m", "2026-05-01T12:00:00-04:00")
        assert "test:m" in {r["model"] for r in db.fetch_dropdown_models(conn)}
        with db.transaction(conn):
            assert db.set_model_deleted(conn, model="test:m", deleted=True) == 1
        # Gone from the dropdown (forward-use honors deleted)...
        assert "test:m" not in {r["model"] for r in db.fetch_dropdown_models(conn)}
        # ...but spend still prices its history (pricing ignores deleted).
        by = {r["model"]: r for r in db.fetch_spend(conn)}
        assert by["test:m"]["cost"] == pytest.approx(1.0)
    finally:
        conn.close()


def test_set_price_deleted_drops_only_priced_model_but_prices_history(tmp_path):
    """Soft-deleting a model's ONLY open window removes it from the dropdown (no
    current price) yet spend still prices its past invocations."""
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            _mk_model(conn, "test:m")
            _price_window(conn, "test:m", 2.0, 0.0, "2026-01-01", None)
            _invocation(conn, "test:m", "2026-05-01T12:00:00-04:00")
        pid = conn.execute(
            "SELECT id FROM model_prices WHERE model='test:m'").fetchone()["id"]
        assert "test:m" in {r["model"] for r in db.fetch_dropdown_models(conn)}
        with db.transaction(conn):
            assert db.set_price_deleted(conn, price_id=pid, deleted=True) == 1
        assert "test:m" not in {r["model"] for r in db.fetch_dropdown_models(conn)}
        by = {r["model"]: r for r in db.fetch_spend(conn)}
        assert by["test:m"]["cost"] == pytest.approx(2.0)  # deleted window prices
    finally:
        conn.close()


def test_fetch_prices_and_latest_window_are_deleted_agnostic(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            _price_window(conn, "test:m", 1.0, 0.0, "2026-01-01", "2026-06-01")
            _price_window(conn, "test:m", 2.0, 0.0, "2026-06-01", None, deleted=1)
        live = db.fetch_prices(conn, "test:m")
        assert [r["valid_from"] for r in live] == ["2026-01-01"]  # deleted hidden
        allp = db.fetch_prices(conn, "test:m", include_deleted=True)
        assert len(allp) == 2
        # latest is deleted-agnostic: the deleted 2026-06-01 window is the latest,
        # so the overlap guard sees it.
        assert db.latest_price_window(conn, "test:m")["valid_from"] == "2026-06-01"
    finally:
        conn.close()


def test_insert_price_window_auto_closes_prior_open(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            _price_window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
        with db.transaction(conn):
            new_id = db.insert_price_window(
                conn, model="test:m", input_per_1m=3.0, output_per_1m=4.0,
                valid_from="2026-06-01", now="2026-06-01T00:00:00-04:00")
        rows = db.fetch_prices(conn, "test:m")
        by_from = {r["valid_from"]: r for r in rows}
        assert by_from["2026-01-01"]["valid_to"] == "2026-06-01"  # auto-closed
        assert by_from["2026-06-01"]["valid_to"] is None          # new open
        assert by_from["2026-06-01"]["id"] == new_id
    finally:
        conn.close()


def test_insert_price_window_deleted_agnostic_prevents_double_count(tmp_path):
    """The load-bearing regression: a soft-deleted-but-OPEN window must also be
    auto-closed, or it plus the new window both cover future dates and the spend
    JOIN double-counts (spend ignores deleted)."""
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            # An OPEN window that is soft-deleted, plus a future invocation.
            _price_window(conn, "test:m", 1.0, 0.0, "2026-01-01", None, deleted=1)
            _invocation(conn, "test:m", "2026-09-01T12:00:00-04:00")
        with db.transaction(conn):
            db.insert_price_window(
                conn, model="test:m", input_per_1m=3.0, output_per_1m=0.0,
                valid_from="2026-06-01", now="2026-06-01T00:00:00-04:00")
        # The deleted-open window was closed at 2026-06-01, not left open. (Scope to
        # test:m — the seeded anthropic window is also priced 1.0.)
        closed = conn.execute(
            "SELECT valid_to FROM model_prices "
            "WHERE model='test:m' AND input_per_1m=1.0").fetchone()
        assert closed["valid_to"] == "2026-06-01"
        # One invocation, covered by exactly one window -> no double-count.
        row = db.fetch_spend(conn)[0]
        assert row["invocations"] == 1          # not 2
        assert row["input_tokens"] == 1_000_000  # not doubled
        assert row["cost"] == pytest.approx(3.0)  # only the new window
    finally:
        conn.close()


def test_count_invocations_for_model_and_in_window(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            _invocation(conn, "test:m", "2026-01-15T12:00:00-05:00")
            _invocation(conn, "test:m", "2026-02-01T12:00:00-05:00")
            _invocation(conn, "test:m", "2026-02-15T12:00:00-05:00")
        assert db.count_invocations_for_model(conn, "test:m") == 3
        # Half-open [2026-01-01, 2026-02-01): includes 01-15, EXCLUDES 02-01.
        assert db.count_invocations_in_window(
            conn, "test:m", "2026-01-01", "2026-02-01") == 1
        # Open [2026-02-01, NULL): includes 02-01 (== valid_from) and 02-15.
        assert db.count_invocations_in_window(
            conn, "test:m", "2026-02-01", None) == 2
    finally:
        conn.close()


def test_preference_set_get_and_overwrite(tmp_path):
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        # Absent key -> None.
        assert db.get_preference(conn, "prompt_fields") is None
        with db.transaction(conn):
            db.set_preference(conn, "prompt_fields", '["view_count"]',
                              "2026-06-10T10:00:00-04:00")
        assert db.get_preference(conn, "prompt_fields") == '["view_count"]'
        # Same key overwrites (one row, new value).
        with db.transaction(conn):
            db.set_preference(conn, "prompt_fields", '["like_count","comment_count"]',
                              "2026-06-10T10:05:00-04:00")
        assert db.get_preference(conn, "prompt_fields") == '["like_count","comment_count"]'
        assert conn.execute(
            "SELECT count(*) c FROM app_preferences").fetchone()["c"] == 1
    finally:
        conn.close()
