import sqlite3

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
    "price_proposals",
    "price_cross_check",
    "run_summary",
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
        "first_10_sec", "saves", "status", "status_changed_at",
        "last_view_growth_at",
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
    "price_proposals": {
        "id", "model", "field", "old_value", "new_value", "pct_change",
        "direction", "quote", "source_url", "status", "proposed_at", "resolved_at",
    },
    "run_summary": {
        "run_id", "run_date", "mode", "added", "updated", "aged_out", "gone",
        "classify_cost", "snapshot_count", "created_at",
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
        assert version == db.SCHEMA_VERSION == 13
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
            "xai:grok-4.3", "google:gemini-2.5-flash-lite",
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
        grok = models["xai:grok-4.3"]
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
                "WHERE model = 'xai:grok-4.3'"
            )
        before = conn.execute(
            "SELECT count(*) c FROM model_prices WHERE model = 'xai:grok-4.3'"
        ).fetchone()["c"]
    finally:
        conn.close()

    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT deleted, enabled, notes FROM models WHERE model = 'xai:grok-4.3'"
        ).fetchone()
        assert row["deleted"] == 1 and row["enabled"] == 0
        assert row["notes"] == "retired"  # operator edit preserved, not re-seeded
        after = conn.execute(
            "SELECT count(*) c FROM model_prices WHERE model = 'xai:grok-4.3'"
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
    512->5000 fix), local -> NULL (uncapped). The max_tokens ALTER is column-guarded,
    not version-gated, so it still applies on the 8 -> current jump; init_db stamps the
    current SCHEMA_VERSION (11)."""
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
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION

        def mt(model):
            return conn.execute(
                "SELECT max_tokens FROM models WHERE model = ?", (model,)
            ).fetchone()["max_tokens"]

        assert mt("anthropic:claude-haiku-4-5") == config.DEFAULT_MAX_TOKENS
        assert mt("openai:gpt-5.4-nano") == config.DEFAULT_MAX_TOKENS_REASONING
        assert mt("ollama:qwen3.5:9b") is None
    finally:
        conn.close()


# --- price_cross_check (v11): per-run cross-check provenance snapshots -------

def test_cross_check_latest_returns_newest_run(tmp_path):
    db_path = str(tmp_path / "cc.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        def _ins():
            with db.transaction(conn):
                db.insert_cross_check(
                    conn, run_at="2026-06-14T10:00:00-04:00",
                    validator_name="litellm", source_outcomes="[]", cells="{}")
                db.insert_cross_check(
                    conn, run_at="2026-06-14T12:00:00-04:00",
                    validator_name="openrouter",
                    source_outcomes='[{"role": "validator"}]',
                    cells='{"m": 1}')
        db.run_with_db_retry(_ins)
        row = db.latest_cross_check(conn)
        assert row["validator_name"] == "openrouter"       # newest run_at wins
        assert row["run_at"] == "2026-06-14T12:00:00-04:00"
        assert row["cells"] == '{"m": 1}'
        # History is retained (append, not latest-wins overwrite).
        n = conn.execute("SELECT COUNT(*) c FROM price_cross_check").fetchone()["c"]
        assert n == 2
    finally:
        conn.close()


def test_cross_check_latest_none_when_empty(tmp_path):
    db_path = str(tmp_path / "cc.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        assert db.latest_cross_check(conn) is None
    finally:
        conn.close()


def test_cross_check_id_tiebreak_on_equal_run_at(tmp_path):
    # Two rows sharing a run_at: the later INSERT (higher id) is the latest.
    db_path = str(tmp_path / "cc.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        same = "2026-06-14T12:00:00-04:00"

        def _ins():
            with db.transaction(conn):
                db.insert_cross_check(conn, run_at=same, validator_name="litellm",
                                      source_outcomes="[]", cells='{"first": 1}')
                db.insert_cross_check(conn, run_at=same, validator_name="litellm",
                                      source_outcomes="[]", cells='{"second": 1}')
        db.run_with_db_retry(_ins)
        assert db.latest_cross_check(conn)["cells"] == '{"second": 1}'
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
        # grok-4.3 is reasoning-capable (Reasoning: Configurable), so the generous cap.
        assert mt("xai:grok-4.3") == config.DEFAULT_MAX_TOKENS_REASONING
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
                "WHERE model = 'xai:grok-4.3'"
            )
        row = db.fetch_model(conn, "xai:grok-4.3")
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
                "UPDATE models SET deleted = 1 WHERE model = 'xai:grok-4.3'"
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


def test_price_proposals_partial_index_one_pending_per_model_field(tmp_path):
    """ux_pending_proposal allows at most one PENDING row per (model, field); a
    resolved row (confirmed/rejected) is outside the partial index, so it never
    conflicts."""
    import sqlite3
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        def _proposal(status):
            conn.execute(
                "INSERT INTO price_proposals (model, field, status) "
                "VALUES ('test:m', 'input', ?)", (status,))
        with db.transaction(conn):
            _proposal("pending")
        # A second PENDING for the same (model, field) violates the partial index.
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction(conn):
                _proposal("pending")
        # Resolved rows are outside the index — any number coexist.
        with db.transaction(conn):
            _proposal("confirmed")
            _proposal("rejected")
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM price_proposals WHERE model='test:m'"
        ).fetchone()["c"]
        assert cnt == 3
    finally:
        conn.close()


def test_prior_price_window_is_id_tiebroken_and_non_deleted(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            _price_window(conn, "test:m", 1.0, 2.0, "2026-01-01", "2026-06-01")
            _price_window(conn, "test:m", 3.0, 4.0, "2026-06-01", None)
            # a soft-deleted window between them must NOT be the prior.
            _price_window(conn, "test:m", 9.9, 9.9, "2026-03-01", "2026-06-01",
                          deleted=1)
        prior = db.prior_price_window(conn, "test:m", "2026-06-01")
        assert prior["valid_from"] == "2026-01-01"           # skips the deleted 03-01
        assert prior["input_per_1m"] == 1.0
        # nothing strictly before the earliest window.
        assert db.prior_price_window(conn, "test:m", "2026-01-01") is None
    finally:
        conn.close()


def test_active_price_window_is_the_open_non_deleted_window(tmp_path):
    """active_price_window returns the single open (valid_to IS NULL) non-deleted
    window — NOT latest_price_window, which is deleted-agnostic and would surface a
    soft-deleted-but-open row."""
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        assert db.active_price_window(conn, "test:m") is None
        with db.transaction(conn):
            _price_window(conn, "test:m", 1.0, 2.0, "2026-01-01", "2026-06-01")
            _price_window(conn, "test:m", 3.0, 4.0, "2026-06-01", None)
            # A soft-deleted-but-open window must be ignored by active.
            _price_window(conn, "test:m", 9.9, 9.9, "2026-07-01", None, deleted=1)
        active = db.active_price_window(conn, "test:m")
        assert active["valid_from"] == "2026-06-01"
        assert active["input_per_1m"] == 3.0 and active["output_per_1m"] == 4.0
    finally:
        conn.close()


def test_update_price_window_replaces_values_in_place(tmp_path):
    """update_price_window overwrites a window's prices + recorded_at by id, leaving
    valid_from/valid_to untouched (the supersede primitive)."""
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            _price_window(conn, "test:m", 1.0, 2.0, "2026-06-01", None)
        win = db.active_price_window(conn, "test:m")
        with db.transaction(conn):
            rows = db.update_price_window(
                conn, price_id=win["id"], input_per_1m=5.0, output_per_1m=6.0,
                now="2026-06-02T00:00:00-04:00")
        assert rows == 1
        after = db.active_price_window(conn, "test:m")
        assert after["id"] == win["id"]
        assert after["input_per_1m"] == 5.0 and after["output_per_1m"] == 6.0
        assert after["valid_from"] == "2026-06-01" and after["valid_to"] is None
        assert after["recorded_at"] == "2026-06-02T00:00:00-04:00"
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


# The videos CREATE BEFORE v12 (no status / status_changed_at / last_view_growth_at).
# Used to build a genuine v11 DB so the v11->v12 ADD COLUMN + backfill path is
# actually exercised — building it from the CURRENT statement (which already has
# the three columns) would test nothing.
_PRE_V12_VIDEOS = """
CREATE TABLE videos (
    video_id TEXT PRIMARY KEY,
    title TEXT,
    channel_id TEXT,
    channel_title TEXT,
    published_at TEXT,
    duration_seconds INTEGER,
    is_short INTEGER,
    link TEXT,
    thumbnail_url TEXT,
    description TEXT,
    category_id TEXT,
    audio_language TEXT,
    definition TEXT,
    has_captions INTEGER,
    made_for_kids INTEGER,
    tags TEXT,
    topic_categories TEXT,
    top_comments TEXT,
    matched_queries TEXT,
    buckets TEXT,
    view_count INTEGER,
    like_count INTEGER,
    comment_count INTEGER,
    views_to_subs_ratio REAL,
    views_per_day REAL,
    first_seen_at TEXT,
    last_api_refresh_at TEXT,
    user_notes TEXT DEFAULT '',
    starred INTEGER DEFAULT 0,
    starred_at TEXT,
    hook TEXT DEFAULT '',
    first_10_sec TEXT DEFAULT '',
    saves TEXT DEFAULT ''
)
"""


def _build_v11_db(db_path):
    """A real v11 schema: every current CREATE EXCEPT videos at its pre-v12 shape
    (no status / status_changed_at / last_view_growth_at), stamped user_version=11,
    seeded with three videos and a stats_snapshots history that exercises every
    backfill branch:
      - 'vid_grow':   3 snapshots, rose at run 2 then flat at run 3
                      -> last_view_growth_at = run-2 captured_at
      - 'vid_oneshot': 1 snapshot (fewer than two) -> fallback first_seen_at
      - 'vid_flat':   2 snapshots, never rose      -> fallback first_seen_at
    Returns the expected last_view_growth_at per video_id."""
    conn = db.get_connection(db_path)
    try:
        for statement in db.SCHEMA_STATEMENTS:
            if "EXISTS videos" in statement:  # use the pre-v12 videos DDL instead
                continue
            conn.execute(statement)
        conn.execute(_PRE_V12_VIDEOS)
        conn.execute("PRAGMA user_version = 11")

        videos = [
            ("vid_grow", "Grower", "2026-06-08T09:00:00-04:00"),
            ("vid_oneshot", "One Shot", "2026-06-08T08:00:00-04:00"),
            ("vid_flat", "Flatliner", "2026-06-08T07:00:00-04:00"),
        ]
        for vid, title, first_seen in videos:
            conn.execute(
                "INSERT INTO videos (video_id, title, first_seen_at, "
                "last_api_refresh_at) VALUES (?, ?, ?, ?)",
                (vid, title, first_seen, first_seen),
            )
        # (run_id, video_id, captured_at, view_count)
        snaps = [
            (1, "vid_grow", "2026-06-08T10:00:00-04:00", 100),
            (2, "vid_grow", "2026-06-09T10:00:00-04:00", 150),   # rose
            (3, "vid_grow", "2026-06-10T10:00:00-04:00", 150),   # flat
            (1, "vid_oneshot", "2026-06-08T10:00:00-04:00", 200),
            (1, "vid_flat", "2026-06-08T10:00:00-04:00", 300),
            (2, "vid_flat", "2026-06-09T10:00:00-04:00", 300),   # flat
        ]
        for run_id, vid, captured_at, views in snaps:
            conn.execute(
                "INSERT INTO stats_snapshots (run_id, video_id, captured_at, "
                "view_count, like_count, comment_count) VALUES (?, ?, ?, ?, 0, 0)",
                (run_id, vid, captured_at, views),
            )
        conn.commit()
    finally:
        conn.close()
    return {
        "vid_grow": "2026-06-09T10:00:00-04:00",   # captured_at of the run it rose
        "vid_oneshot": "2026-06-08T08:00:00-04:00",  # first_seen_at fallback
        "vid_flat": "2026-06-08T07:00:00-04:00",     # first_seen_at fallback
    }


def test_v11_to_v12_adds_status_and_growth_columns(tmp_path):
    """An existing v11 DB gains videos.status (backfilled 'active' via its NOT NULL
    DEFAULT), status_changed_at (NULL), and last_view_growth_at (seeded from the
    snapshot history), bumps to the current schema, and leaves the seeded rows'
    other fields unchanged."""
    db_path = str(tmp_path / "test.db")
    expected_growth = _build_v11_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert "status" not in _columns(conn, "videos")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
        titles_before = dict(
            conn.execute("SELECT video_id, title FROM videos").fetchall()
        )
    finally:
        conn.close()

    db.init_db(db_path)

    conn = db.get_connection(db_path)
    try:
        assert {"status", "status_changed_at", "last_view_growth_at"}.issubset(
            _columns(conn, "videos")
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        rows = {
            r["video_id"]: r
            for r in conn.execute(
                "SELECT video_id, title, status, status_changed_at, "
                "last_view_growth_at FROM videos"
            ).fetchall()
        }
        for vid, expected in expected_growth.items():
            assert rows[vid]["status"] == "active"
            assert rows[vid]["status_changed_at"] is None
            assert rows[vid]["last_view_growth_at"] == expected, vid
            assert rows[vid]["title"] == titles_before[vid]  # untouched
    finally:
        conn.close()


def test_v11_to_v12_migration_is_atomic(tmp_path):
    """The videos ALTERs and the user_version stamp are one atomic unit: a rollback
    after them must leave version 11 AND no status column, proving the PRAGMA
    participates in the migration transaction rather than committing on its own."""
    db_path = str(tmp_path / "test.db")
    _build_v11_db(db_path)

    conn = db.get_connection(db_path)
    try:
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE videos ADD COLUMN status TEXT NOT NULL "
                     "DEFAULT 'active'")
        conn.execute("PRAGMA user_version = 12")
        conn.execute("ROLLBACK")

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
        assert "status" not in _columns(conn, "videos")
    finally:
        conn.close()


def test_is_view_growth_definition():
    """The shared growth predicate: strictly-higher is growth; equal, lower, and an
    absent prior are all not-growth (so a new video keeps its catch-day clock)."""
    assert db.is_view_growth(100, 150) is True
    assert db.is_view_growth(150, 150) is False
    assert db.is_view_growth(150, 100) is False
    assert db.is_view_growth(None, 150) is False   # no prior snapshot
    assert db.is_view_growth(100, None) is False   # missing fresh count


def _insert_video(conn, video_id, *, status="active", starred=0,
                  last_view_growth_at="2026-06-08T10:00:00-04:00"):
    conn.execute(
        "INSERT INTO videos (video_id, title, channel_id, matched_queries, "
        "buckets, top_comments, first_seen_at, status, starred, "
        "last_view_growth_at) VALUES (?, ?, 'chan', '[]', '[]', '[]', "
        "'2026-06-08T09:00:00-04:00', ?, ?, ?)",
        (video_id, f"T-{video_id}", status, starred, last_view_growth_at),
    )


def test_fetch_videos_for_refresh_returns_only_active(tmp_path):
    """The sweep batch is the active catalog: gone and aged_out are excluded, and
    each row still carries the five preserve columns videos.list won't re-supply."""
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        _insert_video(conn, "act1")
        _insert_video(conn, "act2")
        _insert_video(conn, "gone1", status="gone")
        _insert_video(conn, "aged1", status="aged_out")
        conn.commit()

        rows = db.fetch_videos_for_refresh(conn)
        assert {r["video_id"] for r in rows} == {"act1", "act2"}
        assert set(rows[0].keys()) == {
            "video_id", "channel_id", "matched_queries", "buckets", "top_comments",
        }
    finally:
        conn.close()


def test_count_eligible_for_refresh_counts_only_active(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        _insert_video(conn, "act1")
        _insert_video(conn, "act2")
        _insert_video(conn, "gone1", status="gone")
        _insert_video(conn, "aged1", status="aged_out")
        conn.commit()
        assert db.count_eligible_for_refresh(conn) == 2
    finally:
        conn.close()


def test_fetch_aging_candidates_excludes_starred_and_non_active(tmp_path):
    """Aging candidates are active AND unstarred: a starred-and-stale video stays
    (exempt) and a gone video is never returned, so the pure decision never sees
    them."""
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        _insert_video(conn, "act_unstarred", starred=0)
        _insert_video(conn, "act_starred", starred=1)       # exempt
        _insert_video(conn, "gone1", status="gone")          # excluded
        _insert_video(conn, "aged1", status="aged_out")      # excluded
        conn.commit()

        cands = db.fetch_aging_candidates(conn)
        assert {c["video_id"] for c in cands} == {"act_unstarred"}
        assert set(cands[0].keys()) == {"video_id", "last_view_growth_at"}
    finally:
        conn.close()


def test_set_video_status_transitions_only_on_change(tmp_path):
    """A real transition stamps status_changed_at; re-marking the same status is a
    no-op (rowcount 0) that leaves the original timestamp intact."""
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        _insert_video(conn, "v1")  # status='active', status_changed_at NULL
        conn.commit()

        with db.transaction(conn):
            n = db.set_video_status(conn, "v1", "gone", "2026-06-20T10:00:00-04:00")
        assert n == 1
        row = conn.execute(
            "SELECT status, status_changed_at FROM videos WHERE video_id='v1'"
        ).fetchone()
        assert row["status"] == "gone"
        assert row["status_changed_at"] == "2026-06-20T10:00:00-04:00"

        # Re-marking gone is a no-op: rowcount 0, timestamp NOT overwritten.
        with db.transaction(conn):
            n = db.set_video_status(conn, "v1", "gone", "2026-06-21T10:00:00-04:00")
        assert n == 0
        assert conn.execute(
            "SELECT status_changed_at FROM videos WHERE video_id='v1'"
        ).fetchone()["status_changed_at"] == "2026-06-20T10:00:00-04:00"
    finally:
        conn.close()


def test_bump_view_growth_moves_only_listed_ids(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        old = "2026-06-01T10:00:00-04:00"
        new = "2026-06-20T10:00:00-04:00"
        _insert_video(conn, "v1", last_view_growth_at=old)
        _insert_video(conn, "v2", last_view_growth_at=old)
        conn.commit()

        with db.transaction(conn):
            db.bump_view_growth(conn, ["v1"], new)
        clocks = {r["video_id"]: r["last_view_growth_at"] for r in conn.execute(
            "SELECT video_id, last_view_growth_at FROM videos")}
        assert clocks == {"v1": new, "v2": old}

        # Empty list is a no-op (must not touch anything or raise).
        with db.transaction(conn):
            db.bump_view_growth(conn, [], "2026-07-01T10:00:00-04:00")
        assert conn.execute(
            "SELECT last_view_growth_at FROM videos WHERE video_id='v1'"
        ).fetchone()["last_view_growth_at"] == new
    finally:
        conn.close()


def test_latest_snapshot_view_counts_most_recent_strictly_before(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        for run_id, vc in [(1, 100), (2, 150), (3, 175)]:
            conn.execute(
                "INSERT INTO stats_snapshots (run_id, video_id, captured_at, "
                "view_count, like_count, comment_count) VALUES (?, 'v1', ?, ?, 0, 0)",
                (run_id, f"2026-06-0{run_id}T10:00:00-04:00", vc))
        conn.commit()

        # Strictly before run 3 -> run 2's value.
        assert db.latest_snapshot_view_counts(conn, 3) == {"v1": 150}
        # Strictly before run 2 -> run 1's value.
        assert db.latest_snapshot_view_counts(conn, 2) == {"v1": 100}
        # Strictly before run 1 -> no prior snapshot, absent.
        assert db.latest_snapshot_view_counts(conn, 1) == {}
    finally:
        conn.close()


def test_run_change_counts_partition(tmp_path):
    """The four run-summary counts, partitioned by existed-at-run-start via the
    insert-only first_seen_at: a pre-existing-and-re-swept video is `updated`, never
    `added`, even though it was re-fetched this run."""
    now = "2026-06-25T10:00:00-04:00"
    old = "2026-05-01T10:00:00-04:00"
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        # v_old pre-existed (first_seen=old) and is re-snapshotted this run -> updated.
        _insert_video(conn, "v_old")
        conn.execute("UPDATE videos SET first_seen_at=? WHERE video_id='v_old'", (old,))
        # v_new was inserted this run (first_seen=now) -> added (not also updated).
        _insert_video(conn, "v_new")
        conn.execute("UPDATE videos SET first_seen_at=? WHERE video_id='v_new'", (now,))
        # A gone and an aged_out transition stamped this run.
        _insert_video(conn, "v_gone")
        conn.execute("UPDATE videos SET first_seen_at=?, status='gone', "
                     "status_changed_at=? WHERE video_id='v_gone'", (old, now))
        _insert_video(conn, "v_aged")
        conn.execute("UPDATE videos SET first_seen_at=?, status='aged_out', "
                     "status_changed_at=? WHERE video_id='v_aged'", (old, now))
        for vid in ("v_old", "v_new"):
            conn.execute(
                "INSERT INTO stats_snapshots (run_id, video_id, captured_at, "
                "view_count, like_count, comment_count) VALUES (2, ?, ?, 1, 0, 0)",
                (vid, now))
        conn.commit()

        counts = db.run_change_counts(conn, 2, now)
        assert counts == {"added": 1, "updated": 1, "gone": 1, "aged_out": 1}
    finally:
        conn.close()


def test_discover_ran_today(tmp_path):
    """The once-a-day-cap signal: a success/partial discover completed today
    (Pacific) -> True; a refresh-only today or a discover on another day -> False."""
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        today = config.pacific_date()
        assert db.discover_ran_today(conn, today) is False         # nothing yet

        rid = db.start_run(conn, "refresh", config.now_local_iso())
        db.finish_run(conn, rid, config.now_local_iso(), 0, 0, "success")
        assert db.discover_ran_today(conn, today) is False         # refresh doesn't count

        # A discover dated a different Pacific day must not suppress today (midnight flip).
        old = db.start_run(conn, "discover", "2020-06-01T10:00:00-04:00")
        db.finish_run(conn, old, "2020-06-01T10:05:00-04:00", 0, 0, "success")
        assert db.discover_ran_today(conn, today) is False

        rid = db.start_run(conn, "discover", config.now_local_iso())
        db.finish_run(conn, rid, config.now_local_iso(), 0, 0, "success")
        assert db.discover_ran_today(conn, today) is True          # today's discover counts
    finally:
        conn.close()


def test_write_and_fetch_run_summaries(tmp_path):
    """write_run_summary upserts one row per run_id (ON CONFLICT DO UPDATE updates in
    place, never a second row); fetch is newest-first and respects the limit."""
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        c1 = {"added": 1, "updated": 2, "aged_out": 3, "gone": 4}
        with db.transaction(conn):
            db.write_run_summary(conn, 1, "2026-06-25", "discover", c1, 0.12, 50,
                                 "2026-06-25T10:00:00-04:00")
        row = db.fetch_latest_run_summary(conn)
        assert (row["run_id"], row["mode"], row["added"], row["gone"],
                row["classify_cost"], row["snapshot_count"]) == (1, "discover", 1, 4, 0.12, 50)

        # A second write for run_id=1 UPDATES in place — still exactly one row.
        c2 = {"added": 0, "updated": 9, "aged_out": 0, "gone": 0}
        with db.transaction(conn):
            db.write_run_summary(conn, 1, "2026-06-25", "refresh", c2, 0.0, 60,
                                 "2026-06-25T12:00:00-04:00")
        rows = db.fetch_run_summaries(conn, 10)
        assert len(rows) == 1
        assert rows[0]["mode"] == "refresh" and rows[0]["updated"] == 9

        # newest-first + limit.
        with db.transaction(conn):
            db.write_run_summary(conn, 2, "2026-06-25", "refresh", c2, 0.0, 0,
                                 "2026-06-25T13:00:00-04:00")
        newest = db.fetch_run_summaries(conn, 1)
        assert len(newest) == 1 and newest[0]["run_id"] == 2
    finally:
        conn.close()


def test_classify_cost_in_window_bounds_to_the_run(tmp_path):
    """classify_cost is summed over [run_started, now]: invocations just-before
    run_started and just-after now are excluded; an empty window and a window with no
    classify-scope rows (the refresh case) are exactly 0.0."""
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        today = config.now_local_iso()[:10]            # today-relative -> priced by seed
        before = f"{today}T09:59:59-04:00"
        run_started = f"{today}T10:00:00-04:00"
        in_win = f"{today}T10:30:00-04:00"
        now = f"{today}T11:00:00-04:00"
        after = f"{today}T11:00:01-04:00"
        model = "google:gemini-2.5-flash-lite"         # a seeded, priced model
        scope = "video_classification"
        with db.transaction(conn):
            for ts in (before, in_win, after):
                db.log_invocation(conn, today, scope, model, 0.0, None, None,
                                  1000, 500, ts)

        windowed = db.classify_cost_in_window(conn, scope, run_started, now)
        full = db.classify_cost_in_window(conn, scope, before, after)
        assert windowed > 0                            # the in-window row is priced
        assert abs(full - 3 * windowed) < 1e-9         # boundary excluded exactly 2 of 3
        # An empty window and a wrong-scope window are exactly 0.0.
        assert db.classify_cost_in_window(conn, scope, run_started, run_started) == 0.0
        assert db.classify_cost_in_window(conn, "overall", run_started, now) == 0.0
    finally:
        conn.close()


def test_get_readonly_connection_refuses_writes(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_readonly_connection(db_path)
    try:
        # Reads work...
        assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0
        # ...writes are physically refused by SQLite (mode=ro).
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE _x(a)")
    finally:
        conn.close()


def test_migration_pending(tmp_path):
    db_path = str(tmp_path / "t.db")
    assert db.migration_pending(db_path) is False     # missing file -> not pending
    db.init_db(db_path)
    assert db.migration_pending(db_path) is False      # freshly migrated -> current
    conn = db.get_connection(db_path)
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION - 1}")
    conn.commit()
    conn.close()
    assert db.migration_pending(db_path) is True        # stamped behind -> pending


# --- _velocity_points: one-point-per-day rate (daily collapse, then Δviews/Δdays) ---

def test_velocity_points_collapses_intraday_and_divides_by_real_gap():
    # Two snapshots on the SAME Eastern day (09:00=5000, 23:00=5200) must collapse to
    # one day-total using the LATEST (5200); the intraday pair must NOT be differenced.
    # The next populated day is 2 days later (7200), so the rate is (7200-5200)/2=1000,
    # NOT the raw 2000 delta and NOT a sub-hour-amplified value.
    series = [
        {"captured_at": "2026-06-10T09:00:00-04:00", "view_count": 5000},
        {"captured_at": "2026-06-10T23:00:00-04:00", "view_count": 5200},
        {"captured_at": "2026-06-12T10:00:00-04:00", "view_count": 7200},
    ]
    out = db._velocity_points(series)
    # Only ONE point: 06-10 -> 06-12. The intraday 5000->5200 pair produced no point.
    assert len(out) == 1
    assert out[0]["views_per_day"] == 1000  # (7200 - 5200) / 2-day gap
    assert out[0]["captured_at"] == "2026-06-12T10:00:00-04:00"


def test_velocity_points_buckets_by_eastern_day_boundary():
    # 23:00 ET and 01:00 ET are only 2 hours apart but fall on DIFFERENT Eastern days,
    # so they are a 1-DAY gap, not a 2-hour interval. New rate = (1300-1000)/1 = 300;
    # the old per-snapshot code would have annualized 2 hours into ~3600/day.
    series = [
        {"captured_at": "2026-06-10T23:00:00-04:00", "view_count": 1000},
        {"captured_at": "2026-06-11T01:00:00-04:00", "view_count": 1300},
    ]
    out = db._velocity_points(series)
    assert len(out) == 1
    assert out[0]["views_per_day"] == 300


def test_velocity_points_single_day_has_no_pairs():
    series = [{"captured_at": "2026-06-10T09:00:00-04:00", "view_count": 5000}]
    assert db._velocity_points(series) == []
