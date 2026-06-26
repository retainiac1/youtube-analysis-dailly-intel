import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import config

# Schema version stamped into PRAGMA user_version. Bump this and branch in
# init_db when a future, non-destructive migration is needed.
# v2: added the `categories` reference table (CREATE TABLE IF NOT EXISTS makes
# this non-destructive — an existing v1 DB gains the empty table on next init).
# v3: added the `interpretations` table for the dashboard (same non-destructive
# IF NOT EXISTS path; the seed gains the empty table on next init).
# v4: added the `llm_invocations` table for the interpretation generator (same
# non-destructive IF NOT EXISTS path; the seed gains the empty table on next init).
# v5: added llm_invocations.duration_ms (generator run time). This is the FIRST
# bump that runs ALTER TABLE on an existing table — CREATE IF NOT EXISTS cannot add
# a column — so init_db carries an explicit, atomic ADD COLUMN + stamp branch.
# v6: added the `app_preferences` key-value table (remembered UI choices, e.g. the
# generator's selected prompt fields). Additive IF NOT EXISTS path; the seed gains
# the empty table on next init. The v5 ADD COLUMN branch is guarded, so re-running
# init_db for this bump skips it.
# v7: added the `models` + `model_prices` registry tables (the single source of
# truth for the dropdown, capability flags, and effective-dated prices) and two
# `interpretations` columns (temperature, seed = the applied values). New tables
# are additive IF NOT EXISTS; the two columns are a guarded ALTER in the atomic
# block (like v5's duration_ms). init_db also OFFLINE-seeds the baseline
# models (insert-if-empty) so a freshly-migrated DB is never broken — see
# _seed_models.
# v8: added interpretations.think (the applied think value per run; NULL when the
# model is not reasoning-capable / its adapter does not honor think, 0/1 when it
# does — same nullable-applied-value semantics as temperature and seed). Guarded
# ALTER in the same atomic block. The offline seed gate widens to current_version
# < 8 so an existing v7 DB picks up the newly-added local Ollama model; the seed is
# insert-if-empty (ON CONFLICT DO NOTHING) and soft-deleted rows conflict to a
# no-op, so re-running never resurrects an operator-removed model.
# v9: added models.max_tokens (the per-model output-token cap; NULLABLE, NULL =
# uncapped). Guarded ALTER in the same atomic block, then a reasoning-aware backfill
# of pre-existing non-local rows (config.default_max_tokens, so no literal here) and
# a fail-closed assertion that no non-local/unknown-provider row is left uncapped.
# Local rows (ollama) stay NULL; the "never uncapped" guarantee for paid models is
# enforced by validation, not the column. See config.default_max_tokens.
# v10: added the `price_proposals` table + `ux_pending_proposal` partial unique index
# (the price-refresh agent stages >10% price moves for a one-click human confirm). Both
# are unconditional CREATE IF NOT EXISTS — additive, idempotent, and NOT gated on
# current_version, so a version-number collision cannot strand them the way the
# current_version<8 seed gate could. No data backfill: the table is born empty.
# v11: added the `price_cross_check` table (one row per price-refresh run: the validator
# that answered, the per-URL fetch outcomes, and the per-model/field cross-check cells,
# stored as JSON). An APPEND history (designed for mismatch-trend-over-time), so the
# /prices panel reads the latest with ORDER BY run_at DESC. Unconditional CREATE IF NOT
# EXISTS + an index — additive, idempotent, born empty, not version-gated.
# v12: added videos.status (NOT NULL DEFAULT 'active'), videos.status_changed_at,
# and videos.last_view_growth_at to support the daily full-catalog snapshot sweep.
# The sweep marks videos active/gone/aged_out and tracks a per-video view-growth
# clock so a video that stops growing for REFRESH_MAX_AGE_DAYS is retired. Guarded
# ALTERs in the same atomic block; status backfills to 'active' via its DEFAULT,
# and last_view_growth_at is backfilled once from stats_snapshots (the captured_at
# of the most recent run where view_count rose, else first_seen_at).
# v13: added the `run_summary` table (one row per run: mode, the four catalog-change
# counts, classify_cost, snapshot_count). Unconditional CREATE IF NOT EXISTS —
# additive, idempotent, born empty, not version-gated; the once-a-day discover cap
# reads run_log (discover_ran_today), NOT this table.
SCHEMA_VERSION = 13

SCHEMA_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS videos (
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
        saves TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',  -- v12: active | gone | aged_out
        status_changed_at TEXT,                 -- v12: set on any status transition
        last_view_growth_at TEXT                -- v12: last run whose view_count rose
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS channels (
        channel_id TEXT PRIMARY KEY,
        subscriber_count INTEGER,
        channel_video_count INTEGER,
        channel_total_views INTEGER,
        channel_created_date TEXT,
        channel_country TEXT,
        channel_keywords TEXT,
        last_updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stats_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER,
        video_id TEXT,
        captured_at TEXT,
        view_count INTEGER,
        like_count INTEGER,
        comment_count INTEGER,
        UNIQUE(run_id, video_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rankings (
        run_date TEXT,
        bucket TEXT,
        rank INTEGER,
        video_id TEXT,
        metric_value REAL,
        captured_at TEXT,
        PRIMARY KEY (run_date, bucket, rank)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_log (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        mode TEXT,
        started_at TEXT,
        finished_at TEXT,
        quota_used INTEGER,
        videos_seen INTEGER,
        status TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quota_ledger (
        pacific_date TEXT PRIMARY KEY,
        units_used INTEGER,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS categories (
        category_id TEXT PRIMARY KEY,
        title TEXT,
        region_code TEXT,
        last_updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS interpretations (
        run_date TEXT,
        scope TEXT,
        text TEXT,
        model TEXT,
        generated_at TEXT,
        temperature REAL,      -- v7: the applied temperature (NULL if omitted)
        seed INTEGER,          -- v7: the applied seed (NULL if the provider omitted)
        think INTEGER,         -- v8: the applied think value (NULL = not applicable)
        PRIMARY KEY (run_date, scope)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_invocations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_date TEXT,
        scope TEXT,
        model TEXT,            -- canonical "provider:model"
        temperature REAL,
        seed INTEGER,          -- NULL when unset OR provider does not apply a seed
        filter TEXT,           -- v2 forward-compat; ALWAYS NULL in v1
        input_tokens INTEGER,
        output_tokens INTEGER,
        generated_at TEXT,
        duration_ms INTEGER    -- v5: generator run time (NULL for un-instrumented rows)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app_preferences (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS models (
        model TEXT PRIMARY KEY,         -- canonical "provider:model"
        provider TEXT,
        enabled INTEGER DEFAULT 1,      -- dropdown flag (hide without deleting)
        supports_temperature INTEGER,   -- pass temperature only if 1
        supports_seed INTEGER,          -- pass seed only if 1
        is_reasoning INTEGER,           -- informational / UI
        max_tokens INTEGER,             -- output cap; NULL = uncapped (local only)
        deleted INTEGER DEFAULT 0,      -- soft-delete flag
        added_at TEXT,
        notes TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        model TEXT,                     -- joins models.model
        input_per_1m REAL,
        output_per_1m REAL,
        valid_from TEXT,                -- inclusive, YYYY-MM-DD
        valid_to TEXT,                  -- exclusive; NULL = current open window
        deleted INTEGER DEFAULT 0,      -- soft-delete flag
        recorded_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        model TEXT,
        field TEXT,              -- 'input' or 'output'
        old_value REAL,
        new_value REAL,
        pct_change REAL,
        direction TEXT,          -- 'up' or 'down'
        quote TEXT,              -- the extracted snippet justifying the number
        source_url TEXT,
        status TEXT,             -- 'pending' / 'confirmed' / 'rejected'
        proposed_at TEXT,        -- Eastern ISO-8601 with offset
        resolved_at TEXT         -- Eastern ISO-8601 with offset
    )
    """,
    # At most one PENDING proposal per (model, field): a persisting >10% move upserts
    # this single row rather than stacking a new pending one each day. Partial index —
    # resolved (confirmed/rejected) rows are outside it and never conflict.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_pending_proposal
        ON price_proposals (model, field) WHERE status = 'pending'
    """,
    """
    CREATE TABLE IF NOT EXISTS price_cross_check (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at TEXT,                -- Eastern ISO-8601 with offset (config.now_local_iso)
        validator_name TEXT,        -- which validator answered, or NULL if all failed
        source_outcomes TEXT,       -- JSON: every URL attempted (role/url/ok/reason/...)
        cells TEXT                  -- JSON: per model -> {input/output: {scraped, validator, flag}}
    )
    """,
    # Latest-run lookup for the /prices panel is ORDER BY run_at DESC (id tiebreak); the
    # index keeps that fast as the history grows.
    """
    CREATE INDEX IF NOT EXISTS ix_cross_check_run_at
        ON price_cross_check (run_at DESC)
    """,
    # v13: one row per run summarising what the catalog sweep changed. run_id is the
    # run_log run_id (one summary per run; written by the sweep). Plain INTEGER PK,
    # NOT a declared FK — matching stats_snapshots.run_id (the project does not
    # formalise run_id FKs); the PK alone gives one-row-per-run, and a leaf table
    # with no FK cannot cascade. run_date is Eastern.
    """
    CREATE TABLE IF NOT EXISTS run_summary (
        run_id INTEGER PRIMARY KEY,
        run_date TEXT,                 -- Eastern (run_started[:10])
        mode TEXT,                     -- 'discover' | 'refresh'
        added INTEGER,
        updated INTEGER,
        aged_out INTEGER,
        gone INTEGER,
        classify_cost REAL,            -- USD; 0 on the refresh (classify-free) path
        snapshot_count INTEGER,        -- stats_snapshots rows this run
        created_at TEXT                -- Eastern ISO-8601 with offset
    )
    """,
]


def get_connection(
    db_path: str, *, check_same_thread: bool = True
) -> sqlite3.Connection:
    """Open a SQLite connection with WAL journaling and foreign keys enabled and
    a row factory that yields name-addressable rows. The caller is responsible
    for closing the connection.

    check_same_thread defaults to True (the sqlite3 default), preserving the
    pipeline's single-thread safety net. The dashboard passes False: FastAPI runs
    its sync endpoints and their sync `yield` connection dependency on an anyio
    threadpool, so a per-request connection can be created on one worker thread and
    used on another. That is safe here because each connection is confined to one
    request and used serially (setup -> endpoint -> teardown), never shared
    concurrently across threads."""
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_readonly_connection(db_path: str) -> sqlite3.Connection:
    """Open a strictly READ-ONLY connection (`mode=ro`), for callers that must
    guarantee no writes — notably --dry-run, which must never migrate the live
    seed. SQLite physically refuses any write on this handle. Unlike `immutable=1`,
    `mode=ro` still reads the WAL, so it sees the true current state. Deliberately
    does NOT run `PRAGMA journal_mode=WAL` (that pragma is itself a write and would
    fail on a read-only handle), which is exactly why such callers must use this
    rather than get_connection. Raises sqlite3.OperationalError if the file is
    absent (the caller checks existence first)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migration_pending(db_path: str) -> bool:
    """True when the DB at `db_path` is BEHIND the code's SCHEMA_VERSION (an
    init_db would migrate it). Read-only (mode=ro, sees the WAL). A missing file
    returns False: a fresh DB has no data to lose and is created+migrated by the
    real run (which already backs up or has nothing to back up). Used by
    run-pipeline.sh to force a pre-run backup whenever a migration is pending,
    regardless of --dry-run."""
    if not Path(db_path).exists():
        return False
    conn = get_readonly_connection(db_path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION
    finally:
        conn.close()


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """The column names of `table` (via PRAGMA table_info). Used to make the
    column-add migration idempotent: only ALTER when the column is genuinely
    missing, so re-running init_db on an already-migrated DB is a no-op."""
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _seed_models(conn: sqlite3.Connection) -> None:
    """Offline-seed the baseline model registry from config.SEED_MODELS:
    insert-if-empty one `models` row per model and one OPEN `model_prices` window
    per model that has none. Deterministic and network-free — the live xAI string
    correction is seed_models.py's job, run after migration.

    The single window's valid_from is the earliest existing invocation's Eastern
    date (so every already-logged invocation is covered and priced under the
    unchanged seed prices, no unpriced regression), or today on an empty log. Note
    this backdates ALL history to today's price — correct only because the rates
    have not changed over the log's life; a differing past price would need real
    historical windows (out of scope). ON CONFLICT(model) DO NOTHING and the
    per-model price-existence guard make this idempotent: it never clobbers an
    operator's later edits (a soft-deleted model stays deleted; no duplicate
    window is added)."""
    now = config.now_local_iso()
    earliest = conn.execute(
        "SELECT MIN(substr(generated_at, 1, 10)) AS d FROM llm_invocations"
    ).fetchone()["d"]
    valid_from = earliest if earliest else now[:10]
    for m in config.SEED_MODELS:
        conn.execute(
            "INSERT INTO models (model, provider, enabled, supports_temperature, "
            "supports_seed, is_reasoning, max_tokens, deleted, added_at, notes) "
            "VALUES (:model, :provider, 1, :st, :ss, :ir, :mt, 0, :now, NULL) "
            "ON CONFLICT(model) DO NOTHING",
            {"model": m["model"], "provider": m["provider"],
             "st": m["supports_temperature"], "ss": m["supports_seed"],
             "ir": m["is_reasoning"],
             "mt": config.default_max_tokens(m["provider"], m["is_reasoning"]),
             "now": now},
        )
        has_window = conn.execute(
            "SELECT 1 FROM model_prices WHERE model = ? LIMIT 1", (m["model"],)
        ).fetchone()
        if has_window is None:
            price = config.DEFAULT_PRICES[m["model"]]
            conn.execute(
                "INSERT INTO model_prices (model, input_per_1m, output_per_1m, "
                "valid_from, valid_to, deleted, recorded_at) "
                "VALUES (:model, :inp, :out, :vf, NULL, 0, :now)",
                {"model": m["model"], "inp": price["input"],
                 "out": price["output"], "vf": valid_from, "now": now},
            )


def is_view_growth(prev_view_count, new_view_count) -> bool:
    """The single definition of "growth": a strictly higher view_count than the
    most recent prior snapshot. Used by BOTH the v12 migration backfill and the
    runtime catalog sweep so the two paths can never drift. An absent prior
    (None) is NOT growth (a brand-new video with no preceding snapshot keeps its
    catch-day clock); a None new count is treated as no growth, not a crash."""
    if prev_view_count is None or new_view_count is None:
        return False
    return new_view_count > prev_view_count


def _backfill_view_growth(conn: sqlite3.Connection) -> None:
    """One-time v12 seed of videos.last_view_growth_at from stats_snapshots.

    For each video, walk its snapshots in run order and record the captured_at of
    the most recent run whose view_count strictly exceeded the immediately prior
    snapshot (the same is_view_growth definition the sweep uses at runtime). Fall
    back to first_seen_at when a video has fewer than two snapshots or never
    increased. Runs inside the migration's atomic block."""
    for row in conn.execute("SELECT video_id, first_seen_at FROM videos").fetchall():
        snaps = conn.execute(
            "SELECT captured_at, view_count FROM stats_snapshots "
            "WHERE video_id = ? ORDER BY run_id ASC",
            (row["video_id"],),
        ).fetchall()
        growth_at = None
        prev_vc = None
        for snap in snaps:
            if is_view_growth(prev_vc, snap["view_count"]):
                growth_at = snap["captured_at"]
            prev_vc = snap["view_count"]
        if growth_at is None:
            growth_at = row["first_seen_at"]
        conn.execute(
            "UPDATE videos SET last_view_growth_at = ? WHERE video_id = ?",
            (growth_at, row["video_id"]),
        )


def init_db(db_path: str) -> None:
    """Create all tables if they do not exist and stamp the schema version.

    Fresh and additive (CREATE IF NOT EXISTS) paths are idempotent. The v4->v5
    bump is the first that ALTERs an existing table (CREATE IF NOT EXISTS cannot
    add a column), so it runs in an EXPLICIT transaction: add the column if
    missing, THEN stamp the version, then commit — so user_version is never ahead
    of the schema, and any interruption leaves a state the idempotent column check
    heals on the next run. v7 extends that block with two interpretations ADD
    COLUMNs and the offline model seed, so columns + seed + stamp commit as one
    unit; a partial v7 (new tables created but the block rolled back) re-runs
    cleanly because every step is IF NOT EXISTS / column-guarded / insert-if-empty.
    v8 adds interpretations.think the same guarded way and widens the seed gate to
    current_version < 8 so an existing v7 DB gains the new Ollama model on re-init."""
    # SQLite will not create missing parent directories; ensure they exist.
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    try:
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        # Additive tables: safe to autocommit (idempotent, no existing data touched).
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)

        # Migration + version stamp as ONE atomic unit. An EXPLICIT BEGIN is
        # required: db.transaction / Python's legacy sqlite3 autocommit only opens
        # an implicit transaction before DML (INSERT/UPDATE/DELETE), never before
        # ALTER/PRAGMA, so without this BEGIN both statements would autocommit and
        # a rollback would be a no-op. With it, the ADD COLUMN and the user_version
        # header write roll back together on any failure.
        conn.execute("BEGIN")
        try:
            # v4 -> v5: add llm_invocations.duration_ms to existing tables. A fresh
            # DB already has it from the CREATE above, so the check skips the ALTER.
            if "duration_ms" not in _column_names(conn, "llm_invocations"):
                conn.execute(
                    "ALTER TABLE llm_invocations ADD COLUMN duration_ms INTEGER"
                )
            # v6 -> v7: add interpretations.temperature/seed. A fresh DB already has
            # them from the CREATE above, so the guards skip the ALTERs.
            interp_cols = _column_names(conn, "interpretations")
            if "temperature" not in interp_cols:
                conn.execute(
                    "ALTER TABLE interpretations ADD COLUMN temperature REAL"
                )
            if "seed" not in interp_cols:
                conn.execute("ALTER TABLE interpretations ADD COLUMN seed INTEGER")
            # v7 -> v8: add interpretations.think. A fresh DB already has it from the
            # CREATE above, so the guard skips the ALTER.
            if "think" not in interp_cols:
                conn.execute("ALTER TABLE interpretations ADD COLUMN think INTEGER")
            # The local-provider set, as a parameterized IN clause, reused by the v9
            # backfill and the fail-closed guard. Built from config so the policy has
            # one home and no provider string is hardcoded here.
            _locals = sorted(config.LOCAL_PROVIDERS)
            _local_params = {f"local{i}": p for i, p in enumerate(_locals)}
            _local_in = ", ".join(f":{k}" for k in _local_params)
            # v8 -> v9: add models.max_tokens (nullable; NULL = uncapped). A fresh DB
            # already has it from the CREATE above, so the guard skips the ALTER and
            # the seed below sets each row's value. For a pre-existing DB the ALTER
            # leaves every row NULL, so backfill is reasoning-aware: a non-local row
            # gets its config default (reasoning -> generous, else lean); local rows
            # stay NULL. config values are bound, so no token literal lives here.
            if "max_tokens" not in _column_names(conn, "models"):
                conn.execute("ALTER TABLE models ADD COLUMN max_tokens INTEGER")
                # Plain NOT IN (no COALESCE): a NULL-provider row is intentionally
                # NOT matched (NULL NOT IN (...) is NULL), so it stays NULL and the
                # fail-closed guard below catches it and aborts — a dirty provider is
                # surfaced, not silently capped to a default that may be wrong.
                conn.execute(
                    "UPDATE models SET max_tokens = "
                    "CASE WHEN is_reasoning = 1 THEN :reasoning ELSE :default END "
                    "WHERE max_tokens IS NULL "
                    f"AND provider NOT IN ({_local_in})",
                    {"reasoning": config.DEFAULT_MAX_TOKENS_REASONING,
                     "default": config.DEFAULT_MAX_TOKENS, **_local_params},
                )
            # Offline-seed the baseline model registry, gated to current_version < 8
            # so the v7->v8 migration re-runs the seed and an existing v7 DB picks up
            # the new local Ollama model. Insert-if-empty (ON CONFLICT DO NOTHING):
            # the four prior models are no-ops, soft-deleted rows conflict to a no-op
            # (never resurrected), so re-running adds only genuinely-absent models.
            # Inside this block so seed + columns + stamp are atomic.
            if current_version < 8:
                _seed_models(conn)
            # Fail-closed: a non-local (paid) model must never be left uncapped. A
            # row with a NULL or odd-cased provider would slip the backfill's NOT IN
            # (NULL NOT IN (...) is NULL, not TRUE) and stay NULL, so assert it here
            # over ALL rows before stamping. A violation raises and the except below
            # ROLLBACKs the whole migration (column + version), so init_db refuses to
            # leave a paid model uncapped rather than half-applying.
            uncapped = conn.execute(
                "SELECT COUNT(*) AS c FROM models WHERE max_tokens IS NULL "
                f"AND COALESCE(provider, '') NOT IN ({_local_in})",
                _local_params,
            ).fetchone()["c"]
            if uncapped:
                raise ValueError(
                    f"v9 migration would leave {uncapped} non-local model row(s) "
                    "uncapped (NULL max_tokens with a non-local/unknown provider); "
                    "refusing to stamp the schema version"
                )
            # v11 -> v12: add videos.status / status_changed_at / last_view_growth_at.
            # A fresh DB already has all three from the CREATE above, so the guard
            # skips the ALTERs. status carries a NOT NULL DEFAULT 'active', so the
            # ALTER backfills every existing row to 'active' in one step;
            # status_changed_at stays NULL (we do not know when an existing row
            # became active). last_view_growth_at is added nullable (SQLite cannot
            # ADD COLUMN NOT NULL without a default) and then seeded once from the
            # snapshot history, so the no-growth aging clock starts from real data.
            video_cols = _column_names(conn, "videos")
            if "status" not in video_cols:
                conn.execute(
                    "ALTER TABLE videos ADD COLUMN status TEXT NOT NULL "
                    "DEFAULT 'active'"
                )
            if "status_changed_at" not in video_cols:
                conn.execute(
                    "ALTER TABLE videos ADD COLUMN status_changed_at TEXT"
                )
            if "last_view_growth_at" not in video_cols:
                conn.execute(
                    "ALTER TABLE videos ADD COLUMN last_view_growth_at TEXT"
                )
                _backfill_view_growth(conn)
            # Stamp LAST, so the version is never ahead of the schema.
            if current_version != SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


# --- Column contracts -------------------------------------------------------
# These lists drive the generated upsert SQL so the SET clause and the
# last_api_refresh_at CASE can never drift from each other or from the schema.

# API-owned + latest-stats columns: written on every upsert.
VIDEO_API_COLUMNS = [
    "title", "channel_id", "channel_title", "published_at", "duration_seconds",
    "is_short", "link", "thumbnail_url", "description", "category_id",
    "audio_language", "definition", "has_captions", "made_for_kids", "tags",
    "topic_categories", "top_comments", "matched_queries", "buckets",
    "view_count", "like_count", "comment_count", "views_to_subs_ratio",
    "views_per_day",
]

# User-owned columns: NEVER named in any UPDATE, so a refresh cannot clobber them.
VIDEO_USER_COLUMNS = [
    "user_notes", "starred", "starred_at", "hook", "first_10_sec", "saves",
]

# All channel fields are API-owned.
CHANNEL_COLUMNS = [
    "subscriber_count", "channel_video_count", "channel_total_views",
    "channel_created_date", "channel_country", "channel_keywords",
]

# All category fields are API-owned (region_code reflects the configured source).
CATEGORY_COLUMNS = ["title", "region_code"]


def _build_upsert_sql(table: str, key_col: str, api_columns: list[str],
                      now_columns: list[str], extra_set_clause: str) -> str:
    """Build an INSERT ... ON CONFLICT DO UPDATE statement for upsert_video and
    upsert_channel, which share the same shape.

    `key_col` is the conflict target. `api_columns` are bound from the record and
    refreshed on conflict. `now_columns` are bound to :now on insert only (never
    in the SET). `extra_set_clause` is appended to the SET clause and carries the
    table-specific timestamp logic (the video refresh CASE or the channel
    last_updated_at assignment)."""
    insert_cols = [key_col] + api_columns + now_columns
    value_terms = [f":{c}" for c in ([key_col] + api_columns)] + [":now"] * len(now_columns)
    set_clause = ",\n  ".join(f"{c} = excluded.{c}" for c in api_columns)
    return f"""
        INSERT INTO {table} ({", ".join(insert_cols)})
        VALUES ({", ".join(value_terms)})
        ON CONFLICT({key_col}) DO UPDATE SET
          {set_clause},
          {extra_set_clause}
    """


def upsert_video(conn: sqlite3.Connection, record: dict, now: str) -> None:
    """Insert or update one video, enforcing the user-column contract.

    `record` must hold `video_id` plus every key in VIDEO_API_COLUMNS. On insert,
    first_seen_at and last_api_refresh_at are set to `now`. On conflict, only
    API-owned columns are updated; first_seen_at is preserved (never in the SET)
    and last_api_refresh_at advances to `now` only when some API field actually
    changed. User columns are never named here, so they cannot be overwritten."""
    # Null-safe comparison: IS NOT handles NULL counts correctly (unlike <>).
    diff_clause = "\n     OR ".join(
        f"videos.{c} IS NOT excluded.{c}" for c in VIDEO_API_COLUMNS
    )
    refresh_clause = (
        "last_api_refresh_at = CASE\n"
        f"            WHEN {diff_clause}\n"
        "            THEN :now ELSE videos.last_api_refresh_at END"
    )
    # last_view_growth_at is insert-only (in now_columns, never in the SET): a new
    # catch starts its no-growth clock at first_seen_at (both bind :now here), and a
    # re-catch on the conflict path leaves it untouched. The clock is advanced ONLY
    # by the sweep's bump_view_growth, so re-catching an old video can't reset it.
    sql = _build_upsert_sql(
        "videos", "video_id", VIDEO_API_COLUMNS,
        ["first_seen_at", "last_api_refresh_at", "last_view_growth_at"], refresh_clause,
    )
    conn.execute(sql, {**record, "now": now})


def upsert_channel(conn: sqlite3.Connection, record: dict, now: str) -> None:
    """Insert or update one channel (all fields API-owned). `record` must hold
    `channel_id` plus every key in CHANNEL_COLUMNS; last_updated_at is set to `now`."""
    sql = _build_upsert_sql(
        "channels", "channel_id", CHANNEL_COLUMNS,
        ["last_updated_at"], "last_updated_at = :now",
    )
    conn.execute(sql, {**record, "now": now})


def upsert_category(conn: sqlite3.Connection, record: dict, now: str) -> None:
    """Insert or update one video category (all fields API-owned). `record` must
    hold `category_id` plus every key in CATEGORY_COLUMNS; last_updated_at is set
    to `now`. Mirrors upsert_channel — categories are reference data refreshed on
    every run."""
    sql = _build_upsert_sql(
        "categories", "category_id", CATEGORY_COLUMNS,
        ["last_updated_at"], "last_updated_at = :now",
    )
    conn.execute(sql, {**record, "now": now})


def upsert_interpretation(conn: sqlite3.Connection, run_date: str, scope: str,
                          text: str, model: str, now: str,
                          temperature: float | None = None,
                          seed: int | None = None,
                          think: int | None = None) -> None:
    """Insert or overwrite the single interpretation for (run_date, scope). On
    conflict the text, model, generated_at, and the APPLIED temperature/seed/think
    are replaced (re-running a lane overwrites its summary). `model` is the canonical
    "provider:model" string; temperature/seed/think are the values that actually
    governed the run (NULL when not applicable — e.g. Anthropic's seed, or think on a
    model whose adapter does not honor it). Composite-PK shape, so the SQL is written
    inline rather than via _build_upsert_sql (that helper is shaped for single-key
    tables). Does not commit — the caller wraps it in `transaction`."""
    conn.execute(
        """
        INSERT INTO interpretations
            (run_date, scope, text, model, generated_at, temperature, seed, think)
        VALUES (:run_date, :scope, :text, :model, :now, :temperature, :seed, :think)
        ON CONFLICT(run_date, scope) DO UPDATE SET
            text = excluded.text,
            model = excluded.model,
            generated_at = excluded.generated_at,
            temperature = excluded.temperature,
            seed = excluded.seed,
            think = excluded.think
        """,
        {"run_date": run_date, "scope": scope, "text": text, "model": model,
         "now": now, "temperature": temperature, "seed": seed, "think": think},
    )


# --- Dashboard writes (user-owned columns ONLY) -------------------------------
# The dashboard is the SOLE writer of the VIDEO_USER_COLUMNS. These helpers name
# only user-owned columns, the mirror image of upsert_video's API-owned contract,
# so a dashboard write can never clobber a pipeline-refreshed field. Both return
# cursor.rowcount (0 when no such video) and do NOT commit — the caller wraps them
# in `transaction`, with `run_with_db_retry` on the outside.

def set_user_notes(conn: sqlite3.Connection, video_id: str, notes: str) -> int:
    """Overwrite videos.user_notes for one video (full replace, last-write-wins).
    "No note" is the empty string '', never NULL, to match the column default and
    the has_notes_only read filter. Returns the rows affected (0 if video_id is
    unknown). Does not commit."""
    cur = conn.execute(
        "UPDATE videos SET user_notes = :notes WHERE video_id = :video_id",
        {"notes": notes, "video_id": video_id},
    )
    return cur.rowcount


def set_starred(conn: sqlite3.Connection, video_id: str, starred: bool,
                now: str) -> int:
    """Set videos.starred (and starred_at) for one video. starred_at is `now` on
    star, NULL on unstar, so it tracks the star. The bool is coerced to an explicit
    int (1/0) so the stored value matches the starred_only filter (v.starred = 1),
    never a raw Python bool. Returns rows affected (0 if video_id is unknown). Does
    not commit."""
    cur = conn.execute(
        "UPDATE videos SET starred = :starred, starred_at = :starred_at "
        "WHERE video_id = :video_id",
        {
            "starred": 1 if starred else 0,
            "starred_at": now if starred else None,
            "video_id": video_id,
        },
    )
    return cur.rowcount


def log_invocation(conn: sqlite3.Connection, run_date: str, scope: str,
                   model: str, temperature: float, seed, filter,
                   input_tokens: int, output_tokens: int, now: str,
                   *, duration_ms=None) -> None:
    """Append one row to the llm_invocations log recording the full parameter set
    that produced a generation. Append-only (autoincrement id), so re-running a
    lane adds a new row rather than overwriting. `model` is canonical
    "provider:model"; `seed` is NULL when the provider did not apply one; `filter`
    is NULL in v1. `duration_ms` (keyword-only) is the measured generator run time,
    NULL when not measured. Does not commit — the caller wraps it in `transaction`."""
    conn.execute(
        """
        INSERT INTO llm_invocations
            (run_date, scope, model, temperature, seed, filter,
             input_tokens, output_tokens, generated_at, duration_ms)
        VALUES (:run_date, :scope, :model, :temperature, :seed, :filter,
                :input_tokens, :output_tokens, :now, :duration_ms)
        """,
        {"run_date": run_date, "scope": scope, "model": model,
         "temperature": temperature, "seed": seed, "filter": filter,
         "input_tokens": input_tokens, "output_tokens": output_tokens,
         "now": now, "duration_ms": duration_ms},
    )


def insert_snapshot(conn: sqlite3.Connection, run_id: int, video_id: str,
                    captured_at: str, view_count, like_count, comment_count) -> None:
    """Append one stats snapshot for a video within a run. Keyed to
    (run_id, video_id) with DO NOTHING, so an interrupted-then-resumed run with
    the same run_id writes at most one snapshot per video; a new run_id adds a
    fresh row (one snapshot per refresh)."""
    conn.execute(
        """
        INSERT INTO stats_snapshots
            (run_id, video_id, captured_at, view_count, like_count, comment_count)
        VALUES (:run_id, :video_id, :captured_at, :view_count, :like_count, :comment_count)
        ON CONFLICT(run_id, video_id) DO NOTHING
        """,
        {
            "run_id": run_id, "video_id": video_id, "captured_at": captured_at,
            "view_count": view_count, "like_count": like_count,
            "comment_count": comment_count,
        },
    )


def start_run(conn: sqlite3.Connection, mode: str, started_at: str) -> int:
    """Open a run_log row (status 'running') and return its run_id. Commits so the
    row is durable even if a later phase fails."""
    cur = conn.execute(
        "INSERT INTO run_log (mode, started_at, status) VALUES (?, ?, 'running')",
        (mode, started_at),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, finished_at: str,
               quota_used: int, videos_seen: int, status: str) -> None:
    """Write the terminal state of a run_log row. Commits immediately."""
    conn.execute(
        """
        UPDATE run_log
        SET finished_at = ?, quota_used = ?, videos_seen = ?, status = ?
        WHERE run_id = ?
        """,
        (finished_at, quota_used, videos_seen, status, run_id),
    )
    conn.commit()


def fetch_ranking_pool(conn: sqlite3.Connection) -> list[dict]:
    """Return the tracked-video pool used to compute rankings, as plain dicts
    (not sqlite3.Row). Window filtering is done in Python (swipefile.eligible_pool)
    by parsing datetimes — not in SQL — so the boundary is robust to timestamp
    format drift. The pool is bounded by discovery, so a full fetch is cheap."""
    cur = conn.execute(
        "SELECT video_id, buckets, views_to_subs_ratio, view_count, published_at "
        "FROM videos"
    )
    return [dict(row) for row in cur.fetchall()]


def replace_rankings(conn: sqlite3.Connection, run_date: str, bucket: str,
                     ranked: list[tuple[str, float]], captured_at: str) -> None:
    """Idempotently replace one lane's rankings for `run_date`: delete all rows
    for (run_date, bucket), then insert `ranked` as ranks 1..len(ranked). Does NOT
    commit — the caller wraps this in a transaction. Rows for other dates/buckets
    are untouched, so dated history accumulates. An empty `ranked` writes zero rows
    after the delete (the supported empty-lane state)."""
    conn.execute(
        "DELETE FROM rankings WHERE run_date = ? AND bucket = ?",
        (run_date, bucket),
    )
    conn.executemany(
        "INSERT INTO rankings (run_date, bucket, rank, video_id, metric_value, "
        "captured_at) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (run_date, bucket, rank, video_id, metric_value, captured_at)
            for rank, (video_id, metric_value) in enumerate(ranked, start=1)
        ],
    )


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Run a write-phase atomically: commit on success, roll back on any
    exception (and re-raise) so the DB is never left half-updated."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def run_with_db_retry(fn, *, attempts: int = 3, delay: float = 0.5, sleep=time.sleep):
    """Call fn(), retrying on a transient 'database is locked' OperationalError
    (a dashboard or DB browser may hold a read lock) with a short backoff. Other
    errors and the final lock are re-raised. `sleep` is injectable for tests."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower() and attempt < attempts:
                sleep(delay)
                continue
            raise


# --- Quota ledger (Pacific-date keyed) --------------------------------------

def get_units_used(conn: sqlite3.Connection, pacific_date: str) -> int:
    """Return units consumed so far for `pacific_date` (Pacific calendar date,
    the quota_ledger key). Returns 0 when no row exists yet for that date."""
    row = conn.execute(
        "SELECT units_used FROM quota_ledger WHERE pacific_date = ?",
        (pacific_date,),
    ).fetchone()
    return row["units_used"] if row else 0


def add_quota_units(conn: sqlite3.Connection, pacific_date: str, units: int,
                    now: str) -> None:
    """Increment quota_ledger.units_used for `pacific_date` by `units`, starting a
    fresh row on a new Pacific date. Commits its OWN transaction so an eager flush
    survives a later phase rollback (the ledger records units Google already
    charged, independent of whether the phase succeeds). `now` is the Eastern
    timestamp for updated_at."""
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO quota_ledger (pacific_date, units_used, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(pacific_date) DO UPDATE SET
              units_used = units_used + excluded.units_used,
              updated_at = excluded.updated_at
            """,
            (pacific_date, units, now),
        )


def fetch_runs_by_mode(conn: sqlite3.Connection, mode: str) -> list[sqlite3.Row]:
    """Return run_log rows for a given mode (run_id, started_at, status). The
    caller derives the Pacific date from started_at (db stays Pacific-agnostic)."""
    return conn.execute(
        "SELECT run_id, started_at, status FROM run_log WHERE mode = ?",
        (mode,),
    ).fetchall()


# --- Dashboard read helpers (Phase 0) ---------------------------------------

def fetch_run_dates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return the distinct rankings run_dates, most recent first. run_date is the
    Eastern rankings key; it drives the dashboard run picker."""
    return conn.execute(
        "SELECT DISTINCT run_date FROM rankings ORDER BY run_date DESC"
    ).fetchall()


def count_rankings_for_run_date(conn: sqlite3.Connection, run_date: str) -> int:
    """Return the total number of rankings rows committed for `run_date` (across
    all lanes). Used by swipefile.main() to decide EXIT_NO_ROWS: a clean discover
    that wrote zero rows for its run_date signals the scheduler that nothing
    landed. `replace_rankings` does not return a count, so this authoritative
    re-query is the clean source. run_date is the Eastern rankings key."""
    return conn.execute(
        "SELECT COUNT(*) FROM rankings WHERE run_date = ?",
        (run_date,),
    ).fetchone()[0]


def fetch_lane(
    conn: sqlite3.Connection, run_date: str, bucket: str
) -> list[sqlite3.Row]:
    """Return one lane's leaderboard for (run_date, bucket), ranked ascending.
    LEFT JOIN videos on video_id: the schema declares no FK, so a ranking whose
    video row is missing still returns its rank/metric_value with NULL video
    fields rather than vanishing. bucket is a single value (not delimited TEXT),
    so a plain equality match is correct here."""
    return conn.execute(
        """
        SELECT r.rank,
               r.metric_value,
               v.title,
               v.channel_title,
               v.link,
               v.thumbnail_url,
               v.view_count,
               v.views_to_subs_ratio,
               v.like_count,
               v.comment_count,
               v.views_per_day,
               v.duration_seconds,
               v.published_at,
               v.matched_queries,
               v.top_comments
        FROM rankings r
        LEFT JOIN videos v ON v.video_id = r.video_id
        WHERE r.run_date = ? AND r.bucket = ?
        ORDER BY r.rank ASC
        """,
        (run_date, bucket),
    ).fetchall()


def fetch_interpretation(
    conn: sqlite3.Connection, run_date: str, scope: str
) -> sqlite3.Row | None:
    """Return the interpretations row for (run_date, scope) or None when absent,
    plus `duration_ms` — the run time of the invocation that produced the current
    text. scope holds a bucket value (health / habit / overall). duration_ms is
    NULL when no invocation exists for the row (e.g. the seed's pre-instrumentation
    rows).

    The duration subquery picks the NEWEST llm_invocations row for the same
    (run_date, scope). That is the run behind the current text ONLY because
    synthesize_lane logs the invocation and upserts the interpretation in ONE
    transaction (the pinned write order): the highest invocation id always matches
    the stored text. Do not reorder those writes, or this duration would belong to
    a different run than the displayed text. duration_ms is the LAST selected
    column (appended), so name-indexed consumers are undisturbed. The applied
    temperature/seed/think are also selected for provenance display (NULL when not
    applicable)."""
    return conn.execute(
        "SELECT i.run_date, i.scope, i.text, i.model, i.generated_at, "
        "       i.temperature, i.seed, i.think, "
        "       (SELECT li.duration_ms FROM llm_invocations li "
        "        WHERE li.run_date = i.run_date AND li.scope = i.scope "
        "        ORDER BY li.id DESC LIMIT 1) AS duration_ms "
        "FROM interpretations i WHERE i.run_date = ? AND i.scope = ?",
        (run_date, scope),
    ).fetchone()


def fetch_latest_invocation(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Return the most recent llm_invocations row (model, temperature, seed), or
    None when the log is empty. MAX(id) is the recency proxy (the autoincrement id
    is monotonic), so this never lexically compares generated_at. The dashboard
    reads it to prepopulate the generate controls with the last-used parameters."""
    return conn.execute(
        "SELECT model, temperature, seed FROM llm_invocations "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()


# --- model registry readers -------------------------------------------------
# Two read disciplines on the same tables, NEVER sharing a WHERE clause:
# forward-use (fetch_dropdown_models, fetch_model gating left to callers) HONORS
# enabled/deleted; pricing (fetch_effective_price, fetch_spend) IGNORES them.

def fetch_dropdown_models(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The models offered in the generate dropdown: enabled, not soft-deleted, and
    carrying a current price (an OPEN, non-deleted model_prices window). Forward-use
    discipline — UNLIKE spend, this honors enabled/deleted so a hidden model is
    never offered. Ordered by model for a stable dropdown."""
    return conn.execute(
        "SELECT m.* FROM models m "
        "WHERE m.enabled = 1 AND m.deleted = 0 "
        "AND EXISTS (SELECT 1 FROM model_prices p "
        "            WHERE p.model = m.model AND p.valid_to IS NULL "
        "            AND p.deleted = 0) "
        "ORDER BY m.model"
    ).fetchall()


def fetch_model(conn: sqlite3.Connection, model: str) -> sqlite3.Row | None:
    """All attributes of one model (capability flags, provider, ...) or None. A
    plain read that IGNORES enabled/deleted: the generation/capability paths want
    the row regardless of flags, and forward-use gating lives in
    fetch_dropdown_models, not here."""
    return conn.execute(
        "SELECT * FROM models WHERE model = ?", (model,)
    ).fetchone()


def fetch_effective_price(
    conn: sqlite3.Connection, model: str, on_date: str
) -> sqlite3.Row | None:
    """The model_prices window covering `on_date` (an Eastern YYYY-MM-DD date) for
    `model`, or None when no window covers it.

    THE shared window predicate — the half-open interval
    `valid_from <= on_date AND (valid_to IS NULL OR on_date < valid_to)` — that the
    fetch_spend JOIN replicates and must match exactly (a predicate-agreement test
    guards the two against drift). IGNORES `deleted`: backward pricing prices an
    invocation against whatever window covered its date, deleted or not. On the
    non-overlapping windows the design guarantees, at most one matches; if data ever
    overlaps, the latest valid_from wins here (deterministic), while the JOIN would
    double-count — that asymmetry is itself covered by the overlap-canary test."""
    return conn.execute(
        "SELECT * FROM model_prices "
        "WHERE model = ? AND valid_from <= ? "
        "AND (valid_to IS NULL OR ? < valid_to) "
        "ORDER BY valid_from DESC LIMIT 1",
        (model, on_date, on_date),
    ).fetchone()


# --- model registry editor writes/reads (Phase 2) ---------------------------
# House style: named-param dicts, NO commit (caller wraps in transaction), return
# rowcount / new id, booleans coerced to explicit 1/0, timestamps passed in.

def fetch_models(
    conn: sqlite3.Connection, *, include_deleted: bool = False
) -> list[sqlite3.Row]:
    """Every model row for the editor (all columns), ordered by model. UNLIKE
    fetch_dropdown_models this does NOT require a current price and optionally
    surfaces soft-deleted rows (the show-deleted toggle). Read-only."""
    where = "" if include_deleted else "WHERE deleted = 0"
    return conn.execute(
        f"SELECT * FROM models {where} ORDER BY model"
    ).fetchall()


# Sentinel for "max_tokens argument not supplied" — distinct from an explicit None,
# which is a real value (uncapped). Lets insert_model fall back to the reasoning-
# aware default and update_model leave the column untouched.
_UNSET = object()


def insert_model(conn: sqlite3.Connection, *, model: str, provider: str,
                 enabled: bool, supports_temperature: bool, supports_seed: bool,
                 is_reasoning: bool, notes, now: str, max_tokens=_UNSET) -> int:
    """Insert a new model (insert-if-absent). ON CONFLICT(model) DO NOTHING, so a
    duplicate PK returns rowcount 0 (the endpoint maps that to 409) and never
    overwrites. Booleans coerced to 1/0; added_at = now. Does not commit.

    max_tokens is the output cap (None = uncapped). When omitted it falls back to
    config.default_max_tokens(provider, is_reasoning) — the one place the factory
    default lives — so a new cloud model is never inserted uncapped; pass an explicit
    value (including None for a local model) to override."""
    if max_tokens is _UNSET:
        max_tokens = config.default_max_tokens(provider, is_reasoning)
    cur = conn.execute(
        """
        INSERT INTO models (model, provider, enabled, supports_temperature,
                            supports_seed, is_reasoning, max_tokens, deleted,
                            added_at, notes)
        VALUES (:model, :provider, :enabled, :st, :ss, :ir, :mt, 0, :now, :notes)
        ON CONFLICT(model) DO NOTHING
        """,
        {"model": model, "provider": provider, "enabled": 1 if enabled else 0,
         "st": 1 if supports_temperature else 0, "ss": 1 if supports_seed else 0,
         "ir": 1 if is_reasoning else 0, "mt": max_tokens, "now": now,
         "notes": notes},
    )
    return cur.rowcount


def update_model(conn: sqlite3.Connection, *, model: str, enabled: bool,
                 supports_temperature: bool, supports_seed: bool,
                 is_reasoning: bool, notes, max_tokens=_UNSET) -> int:
    """Update the mutable columns of one model. NEVER sets `model` (the PK is taken
    only from the path and is immutable). Booleans coerced to 1/0. Returns rowcount
    (0 = unknown model). Does not commit.

    max_tokens (None = uncapped) is included in the SET only when supplied, so a
    caller that does not manage the cap leaves the stored value untouched rather than
    wiping it to NULL."""
    sets = ["enabled = :enabled", "supports_temperature = :st",
            "supports_seed = :ss", "is_reasoning = :ir", "notes = :notes"]
    params = {"model": model, "enabled": 1 if enabled else 0,
              "st": 1 if supports_temperature else 0,
              "ss": 1 if supports_seed else 0,
              "ir": 1 if is_reasoning else 0, "notes": notes}
    if max_tokens is not _UNSET:
        sets.append("max_tokens = :mt")
        params["mt"] = max_tokens
    cur = conn.execute(
        f"UPDATE models SET {', '.join(sets)} WHERE model = :model", params
    )
    return cur.rowcount


def set_model_deleted(conn: sqlite3.Connection, *, model: str,
                      deleted: bool) -> int:
    """Flip a model's soft-delete flag (hides it from the dropdown; spend still
    prices its history). Returns rowcount (0 = unknown model). Does not commit."""
    cur = conn.execute(
        "UPDATE models SET deleted = :deleted WHERE model = :model",
        {"model": model, "deleted": 1 if deleted else 0},
    )
    return cur.rowcount


def fetch_prices(conn: sqlite3.Connection, model: str, *,
                 include_deleted: bool = False) -> list[sqlite3.Row]:
    """All price windows for one model, ordered by valid_from. Optionally surfaces
    soft-deleted windows (the show-deleted toggle). Read-only."""
    clause = "" if include_deleted else "AND deleted = 0"
    return conn.execute(
        f"SELECT * FROM model_prices WHERE model = ? {clause} ORDER BY valid_from",
        (model,),
    ).fetchall()


def latest_price_window(
    conn: sqlite3.Connection, model: str
) -> sqlite3.Row | None:
    """The window with the greatest valid_from for `model`, across ALL windows
    INCLUDING soft-deleted ones — the deleted-agnostic overlap guard. Because spend
    ignores `deleted`, a soft-deleted-but-open window still prices future spend, so
    add-price must guard (and auto-close) against it too. Read-only."""
    return conn.execute(
        "SELECT * FROM model_prices WHERE model = ? "
        "ORDER BY valid_from DESC LIMIT 1",
        (model,),
    ).fetchone()


def active_price_window(
    conn: sqlite3.Connection, model: str
) -> sqlite3.Row | None:
    """The single OPEN, NON-DELETED window for `model` (valid_to IS NULL AND
    deleted = 0), or None. By the non-overlap invariant there is at most one. Unlike
    latest_price_window (deleted-agnostic, for the overlap guard), this is the current
    EFFECTIVE price — the carry-forward and idempotency baseline the price-refresh
    seam supersedes. Read-only."""
    return conn.execute(
        "SELECT * FROM model_prices "
        "WHERE model = ? AND valid_to IS NULL AND deleted = 0 "
        "ORDER BY valid_from DESC LIMIT 1",
        (model,),
    ).fetchone()


def prior_price_window(
    conn: sqlite3.Connection, model: str, before: str
) -> sqlite3.Row | None:
    """The most recent NON-DELETED window for `model` strictly before `before` (a
    YYYY-MM-DD valid_from), or None. The day-over-day diff baseline for the prices
    panel. Ordered by (valid_from DESC, id DESC) so the prior is unambiguous even if
    two windows ever shared a valid_from (the id tiebreak), rather than relying on
    fetch_prices' valid_from-only ordering. Read-only."""
    return conn.execute(
        "SELECT * FROM model_prices "
        "WHERE model = ? AND deleted = 0 AND valid_from < ? "
        "ORDER BY valid_from DESC, id DESC LIMIT 1",
        (model, before),
    ).fetchone()


def insert_price_window(conn: sqlite3.Connection, *, model: str,
                        input_per_1m: float, output_per_1m: float,
                        valid_from: str, now: str) -> int:
    """Auto-close EVERY open window for `model` (deleted or not) at `valid_from`,
    then insert the new open window. Deleted-agnostic close is load-bearing: a
    soft-deleted-but-open window left open would overlap the new one and the spend
    JOIN would double-count (spend ignores deleted). Returns the new row id. SQL
    only — the endpoint owns validation (date shape, non-negative, the
    valid_from > latest.valid_from overlap guard). Does not commit."""
    conn.execute(
        "UPDATE model_prices SET valid_to = :valid_from "
        "WHERE model = :model AND valid_to IS NULL",
        {"model": model, "valid_from": valid_from},
    )
    cur = conn.execute(
        """
        INSERT INTO model_prices (model, input_per_1m, output_per_1m,
                                  valid_from, valid_to, deleted, recorded_at)
        VALUES (:model, :inp, :out, :valid_from, NULL, 0, :now)
        """,
        {"model": model, "inp": input_per_1m, "out": output_per_1m,
         "valid_from": valid_from, "now": now},
    )
    return cur.lastrowid


def update_price_window(conn: sqlite3.Connection, *, price_id: int,
                        input_per_1m: float, output_per_1m: float,
                        now: str) -> int:
    """Replace a window's prices (and recorded_at) IN PLACE by id, leaving
    valid_from/valid_to untouched. The supersede primitive: when a same-day write
    must update today's already-open window rather than append a new one (which would
    trip the strictly-after valid_from guard). Returns rowcount (0 = unknown id).
    Does not commit."""
    cur = conn.execute(
        "UPDATE model_prices SET input_per_1m = :inp, output_per_1m = :out, "
        "recorded_at = :now WHERE id = :price_id",
        {"inp": input_per_1m, "out": output_per_1m, "now": now,
         "price_id": price_id},
    )
    return cur.rowcount


def set_price_deleted(conn: sqlite3.Connection, *, price_id: int,
                      deleted: bool) -> int:
    """Flip a price window's soft-delete flag (the ONLY write allowed to touch an
    existing price row besides the auto-close). Spend ignores the flag, so this
    only affects the dropdown's current-price check and the editor view. Returns
    rowcount (0 = unknown id). Does not commit."""
    cur = conn.execute(
        "UPDATE model_prices SET deleted = :deleted WHERE id = :price_id",
        {"price_id": price_id, "deleted": 1 if deleted else 0},
    )
    return cur.rowcount


# --- price cross-check provenance (Phase B, v11) ----------------------------

def insert_cross_check(conn: sqlite3.Connection, *, run_at: str,
                       validator_name: str | None, source_outcomes: str,
                       cells: str) -> int:
    """Append one cross-check snapshot for a price-refresh run. `source_outcomes` and
    `cells` are pre-serialized JSON strings (the caller owns the shape). validator_name
    is the validator that answered, or None if all feeds failed. Returns the new row id.
    Append-only (history) — never an upsert; the latest read orders by run_at. Does not
    commit."""
    cur = conn.execute(
        """
        INSERT INTO price_cross_check (run_at, validator_name, source_outcomes, cells)
        VALUES (:run_at, :validator_name, :source_outcomes, :cells)
        """,
        {"run_at": run_at, "validator_name": validator_name,
         "source_outcomes": source_outcomes, "cells": cells},
    )
    return cur.lastrowid


def latest_cross_check(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The most recent cross-check snapshot (greatest run_at, id tiebreak), or None when
    no run has recorded one. The /prices panel renders from this and never refetches the
    validators. Read-only."""
    return conn.execute(
        "SELECT * FROM price_cross_check ORDER BY run_at DESC, id DESC LIMIT 1"
    ).fetchone()


def count_invocations_for_model(conn: sqlite3.Connection, model: str) -> int:
    """How many llm_invocations reference `model` — the delete-warning count for a
    model. Read-only."""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM llm_invocations WHERE model = ?", (model,)
    ).fetchone()["c"]


def count_invocations_in_window(conn: sqlite3.Connection, model: str,
                                valid_from: str, valid_to: str | None) -> int:
    """How many of `model`'s invocations fall in [valid_from, valid_to) by Eastern
    date — the delete-warning count for a price window. Uses THE shared half-open
    predicate (valid_to NULL = open) on substr(generated_at,1,10) so the warning
    matches what fetch_spend actually prices. Read-only."""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM llm_invocations "
        "WHERE model = :model AND :valid_from <= substr(generated_at, 1, 10) "
        "AND (:valid_to IS NULL OR substr(generated_at, 1, 10) < :valid_to)",
        {"model": model, "valid_from": valid_from, "valid_to": valid_to},
    ).fetchone()["c"]


def fetch_spend(
    conn: sqlite3.Connection, *, run_date: str | None = None,
    month_prefix: str | None = None,
) -> list[sqlite3.Row]:
    """Per-model token totals AND dollar cost from llm_invocations, optionally scoped
    to one run_date and/or one Eastern calendar month. One row per model: summed
    input/output tokens, the invocation COUNT, the summed `cost` (priced per
    invocation against its effective price window), and `unpriced_invocations` (rows
    no window covered). Ordered by model.

    Pricing is per-invocation then summed: each row LEFT JOINs the model_prices
    window covering its generated_at Eastern date via THE shared half-open predicate
    `valid_from <= D AND (valid_to IS NULL OR D < valid_to)`, D =
    substr(generated_at,1,10) — the same predicate fetch_effective_price uses (a
    predicate-agreement test guards the two against drift). The JOIN IGNORES
    deleted/enabled on both tables: backward pricing prices any invocation against
    whatever window covered its date, deleted or not. A row no window covers gets a
    NULL price term, so SUM skips its cost (excluded from the total, the display
    'unavailable' path) and unpriced_invocations counts it.

    Correctness ASSUMES non-overlapping windows (Phase 1 seeds one open window per
    model; Phase 2's add-price auto-closes the prior). If windows ever overlap, a
    row matches more than one and its tokens AND cost are DOUBLE-counted here — an
    overlap-canary test documents that consequence. PERFORMANCE: substr() on the
    joined column is non-sargable, so this is a full scan of llm_invocations per
    spend call with no usable index — fine at today's volume, noted for a future
    100k-row self.

    COALESCE(...,0) so a NULL token count contributes 0 rather than nulling a SUM.
    The two filters use deliberately DIFFERENT time keys (do not collapse them):
    `run_date` is the pipeline run a generation summarized; `month_prefix` matches
    substr(generated_at, 1, 7), the Eastern calendar month spend was incurred in
    (generated_at is Eastern via now_local_iso, so its YYYY-MM prefix IS that month —
    an equality on a derived local-month key, never a cross-offset lexical compare)."""
    clauses, params = [], []
    if run_date is not None:
        clauses.append("i.run_date = ?")
        params.append(run_date)
    if month_prefix is not None:
        clauses.append("substr(i.generated_at, 1, 7) = ?")
        params.append(month_prefix)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return conn.execute(
        "SELECT i.model AS model, "
        "SUM(COALESCE(i.input_tokens, 0))  AS input_tokens, "
        "SUM(COALESCE(i.output_tokens, 0)) AS output_tokens, "
        "COUNT(*) AS invocations, "
        "SUM(COALESCE(i.input_tokens, 0)  / 1000000.0 * p.input_per_1m "
        "  + COALESCE(i.output_tokens, 0) / 1000000.0 * p.output_per_1m) AS cost, "
        "SUM(CASE WHEN p.id IS NULL THEN 1 ELSE 0 END) AS unpriced_invocations "
        "FROM llm_invocations i "
        "LEFT JOIN model_prices p "
        "       ON p.model = i.model "
        "      AND p.valid_from <= substr(i.generated_at, 1, 10) "
        "      AND (p.valid_to IS NULL OR substr(i.generated_at, 1, 10) < p.valid_to) "
        f"{where} GROUP BY i.model ORDER BY i.model",
        params,
    ).fetchall()


def get_preference(conn: sqlite3.Connection, key: str) -> str | None:
    """Return the stored value for a UI preference key, or None when unset.
    Values are opaque TEXT (the caller decides the encoding, e.g. a JSON list for
    the generator's selected prompt fields). Read-only."""
    row = conn.execute(
        "SELECT value FROM app_preferences WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row is not None else None


def set_preference(conn: sqlite3.Connection, key: str, value: str,
                   now: str) -> None:
    """Upsert a UI preference (insert, or overwrite value/updated_at on conflict).
    Does not commit — the caller wraps it in `transaction`."""
    conn.execute(
        """
        INSERT INTO app_preferences (key, value, updated_at)
        VALUES (:key, :value, :now)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                       updated_at = excluded.updated_at
        """,
        {"key": key, "value": value, "now": now},
    )


# --- Refresh support --------------------------------------------------------

def fetch_videos_for_refresh(conn: sqlite3.Connection) -> list[dict]:
    """Return, for every ACTIVE tracked video, the fields a sweep must PRESERVE
    (videos.list will not re-supply them): video_id, channel_id, matched_queries,
    buckets, top_comments. Stats are re-fetched from the API, not read here.

    Scoped to status = 'active': `gone` videos (removed from YouTube) and
    `aged_out` videos (retired by the no-growth rule) are excluded so the sweep
    batch stays bounded. The aging pass runs after this fetch each sweep, so
    'active' is the complete eligibility test here (no window/starred predicate
    needed). Every row is 'active' immediately after the v12 backfill, so this is
    behaviour-preserving for an existing catalog."""
    cur = conn.execute(
        "SELECT video_id, channel_id, matched_queries, buckets, top_comments "
        "FROM videos WHERE status = 'active'"
    )
    return [dict(row) for row in cur.fetchall()]


def count_eligible_for_refresh(conn: sqlite3.Connection) -> int:
    """Return the number of videos a sweep would batch: the ACTIVE catalog. Same
    `status = 'active'` predicate as fetch_videos_for_refresh, so the pre-flight
    quota estimate and the actual batch can never disagree. Cheap COUNT."""
    return conn.execute(
        "SELECT COUNT(*) FROM videos WHERE status = 'active'"
    ).fetchone()[0]


def fetch_aging_candidates(conn: sqlite3.Connection) -> list[dict]:
    """Return (video_id, last_view_growth_at) for every video eligible to be aged
    out: status = 'active' AND starred = 0. Starred videos are exempt (never
    retired) and non-active rows (gone / already aged_out) are excluded, so these
    rules live here and the pure decision (swipefile.videos_to_age_out) never has
    to know them. The caller decides which of these to retire by comparing the
    growth clock to the cutoff."""
    cur = conn.execute(
        "SELECT video_id, last_view_growth_at FROM videos "
        "WHERE status = 'active' AND starred = 0"
    )
    return [dict(row) for row in cur.fetchall()]


def latest_snapshot_view_counts(
    conn: sqlite3.Connection, before_run_id: int
) -> dict[str, int]:
    """Return {video_id: view_count} from each video's most recent stats_snapshots
    row STRICTLY BEFORE `before_run_id`. The single prior-snapshot source for the
    sweep's growth compare: the sweep reads this once before writing its own
    snapshots, so it can never pick up the row it is about to write. A video with no
    prior snapshot is simply absent (so is_view_growth sees prev=None -> no growth)."""
    cur = conn.execute(
        """
        SELECT s.video_id, s.view_count
        FROM stats_snapshots s
        JOIN (
            SELECT video_id, MAX(run_id) AS mr
            FROM stats_snapshots
            WHERE run_id < :before
            GROUP BY video_id
        ) m ON s.video_id = m.video_id AND s.run_id = m.mr
        """,
        {"before": before_run_id},
    )
    return {row["video_id"]: row["view_count"] for row in cur.fetchall()}


def bump_view_growth(
    conn: sqlite3.Connection, video_ids: list[str], now: str
) -> None:
    """Advance last_view_growth_at to `now` for each video that grew this run (the
    sweep's grown set). The ONLY writer of the growth clock after insert. executemany
    sidesteps the SQL IN-parameter limit; a no-op on an empty list. Does not commit
    (the caller wraps it in a transaction, so aging in the same txn sees the bump)."""
    if not video_ids:
        return
    conn.executemany(
        "UPDATE videos SET last_view_growth_at = ? WHERE video_id = ?",
        [(now, vid) for vid in video_ids],
    )


def set_video_status(
    conn: sqlite3.Connection, video_id: str, new_status: str, changed_at: str
) -> int:
    """Transition one video to `new_status`, stamping status_changed_at = changed_at.
    The `AND status != :new` guard makes the write a no-op when the status already
    matches, so status_changed_at advances ONLY on a real transition (a gone row's
    disappearance timestamp is never overwritten by a redundant re-mark). Returns
    rows affected (0 when already in that status or video_id unknown). Does not
    commit (caller wraps in a transaction)."""
    cur = conn.execute(
        "UPDATE videos SET status = :new, status_changed_at = :at "
        "WHERE video_id = :vid AND status != :new",
        {"new": new_status, "at": changed_at, "vid": video_id},
    )
    return cur.rowcount


def run_change_counts(
    conn: sqlite3.Connection, run_id: int, now: str
) -> dict[str, int]:
    """The four catalog-change counts for one run, for the run-summary line.
    Partitioned by existed-at-run-start via the insert-only first_seen_at, so a
    just-caught video is `added` only (never also `updated`) despite the sweep's
    redundant re-fetch. `now` is the run's single write instant (shared by the catch
    persist and the sweep). Read-only.

    - added:    rows first inserted this run (first_seen_at == now)
    - updated:  pre-existing rows re-snapshotted this run (a snapshot for run_id,
                first_seen_at != now)
    - aged_out / gone: rows transitioned to that status this run (status_changed_at == now)"""
    def count(sql: str, params: dict) -> int:
        return conn.execute(sql, params).fetchone()[0]

    added = count(
        "SELECT COUNT(*) FROM videos WHERE first_seen_at = :now", {"now": now}
    )
    updated = count(
        "SELECT COUNT(DISTINCT s.video_id) FROM stats_snapshots s "
        "JOIN videos v ON v.video_id = s.video_id "
        "WHERE s.run_id = :rid AND v.first_seen_at != :now",
        {"rid": run_id, "now": now},
    )
    gone = count(
        "SELECT COUNT(*) FROM videos WHERE status = 'gone' "
        "AND status_changed_at = :now", {"now": now}
    )
    aged_out = count(
        "SELECT COUNT(*) FROM videos WHERE status = 'aged_out' "
        "AND status_changed_at = :now", {"now": now}
    )
    return {"added": added, "updated": updated, "aged_out": aged_out, "gone": gone}


def discover_ran_today(conn: sqlite3.Connection, today_pacific: str) -> bool:
    """True when a discover run with status 'success' or 'partial' COMPLETED today
    (Pacific). The single source for the once-a-day-discover cap, read by BOTH the
    pipeline resolver and the dashboard's /api/run-state. Pacific-correct: run_log
    stores Eastern `started_at`, converted here via config.pacific_date and compared
    to the caller-supplied `today_pacific` — NOT a lexical Eastern==Pacific compare."""
    for row in conn.execute(
        "SELECT started_at, status FROM run_log WHERE mode = 'discover'"
    ):
        if (row["status"] in ("success", "partial")
                and config.pacific_date(row["started_at"]) == today_pacific):
            return True
    return False


def write_run_summary(conn: sqlite3.Connection, run_id: int, run_date: str,
                      mode: str, counts: dict, classify_cost: float,
                      snapshot_count: int, created_at: str) -> None:
    """Upsert one run_summary row for `run_id` (idempotent via ON CONFLICT DO
    UPDATE, NOT INSERT OR REPLACE — no delete/re-insert, so no cascade risk; the row
    is a leaf anyway). `counts` is run_change_counts' dict. Does not commit — the
    caller wraps it in a transaction."""
    conn.execute(
        """
        INSERT INTO run_summary
            (run_id, run_date, mode, added, updated, aged_out, gone,
             classify_cost, snapshot_count, created_at)
        VALUES (:run_id, :run_date, :mode, :added, :updated, :aged_out, :gone,
                :classify_cost, :snapshot_count, :created_at)
        ON CONFLICT(run_id) DO UPDATE SET
            run_date = excluded.run_date, mode = excluded.mode,
            added = excluded.added, updated = excluded.updated,
            aged_out = excluded.aged_out, gone = excluded.gone,
            classify_cost = excluded.classify_cost,
            snapshot_count = excluded.snapshot_count, created_at = excluded.created_at
        """,
        {"run_id": run_id, "run_date": run_date, "mode": mode,
         "added": counts["added"], "updated": counts["updated"],
         "aged_out": counts["aged_out"], "gone": counts["gone"],
         "classify_cost": classify_cost, "snapshot_count": snapshot_count,
         "created_at": created_at},
    )


def classify_cost_in_window(conn: sqlite3.Connection, scope: str,
                            start_iso: str, end_iso: str) -> float:
    """Total USD cost of llm_invocations in `scope` whose generated_at falls in the
    INCLUSIVE window [start_iso, end_iso]. Priced with the SAME per-invocation
    model_prices half-open join fetch_spend uses, so the number agrees with the
    spend view. The window (a run's [run_started, now] wall-clock span) bounds cost
    to THIS run: a refresh's window contains no classify calls -> 0.0. Read-only.

    Both bounds are Eastern ISO-8601 with offset; the comparison is on the full
    timestamp string, which is safe here because both ends come from the same run's
    now_local_iso() (no cross-offset compare)."""
    row = conn.execute(
        "SELECT COALESCE(SUM("
        "  COALESCE(i.input_tokens, 0)  / 1000000.0 * p.input_per_1m "
        "+ COALESCE(i.output_tokens, 0) / 1000000.0 * p.output_per_1m), 0.0) AS cost "
        "FROM llm_invocations i "
        "LEFT JOIN model_prices p "
        "       ON p.model = i.model "
        "      AND p.valid_from <= substr(i.generated_at, 1, 10) "
        "      AND (p.valid_to IS NULL OR substr(i.generated_at, 1, 10) < p.valid_to) "
        "WHERE i.scope = :scope "
        "  AND i.generated_at >= :start AND i.generated_at <= :end",
        {"scope": scope, "start": start_iso, "end": end_iso},
    ).fetchone()
    return row["cost"]


def count_snapshots_for_run(conn: sqlite3.Connection, run_id: int) -> int:
    """Number of stats_snapshots rows written for `run_id`. Read-only."""
    return conn.execute(
        "SELECT COUNT(*) FROM stats_snapshots WHERE run_id = ?", (run_id,)
    ).fetchone()[0]


def fetch_latest_run_summary(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The most recent run_summary row (highest run_id), or None when empty. The
    dashboard reads this after a run's child process exits to render its counts."""
    return conn.execute(
        "SELECT * FROM run_summary ORDER BY run_id DESC LIMIT 1"
    ).fetchone()


def fetch_run_summaries(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """The `limit` most recent run_summary rows, newest first. Read-only."""
    return conn.execute(
        "SELECT * FROM run_summary ORDER BY run_id DESC LIMIT ?", (limit,)
    ).fetchall()


def fetch_channel_subs(conn: sqlite3.Connection) -> dict[str, int]:
    """Return {channel_id: subscriber_count} for the views_to_subs_ratio recompute
    during --refresh (which does not re-fetch channels)."""
    cur = conn.execute("SELECT channel_id, subscriber_count FROM channels")
    return {row["channel_id"]: row["subscriber_count"] for row in cur.fetchall()}


# --- Dashboard read helpers (Phase 1: library browse + filters) -------------

# Duration bands (seconds) for the dashboard duration filter. Phase 1 defines
# them HERE (dep-light: db.py + config.py only) rather than importing
# swipefile.py — see the no-swipefile-import invariant. The shared-constants
# move (Phase 2) should fold these into the shared module. The "short" upper
# edge is bound to config.SHORT_MAX_SECONDS so it can never disagree with the
# pipeline's is_short derivation. `None` upper bound = open-ended. Dict order is
# the display order.
DURATION_BANDS: dict[str, tuple[int, int | None]] = {
    "short": (0, config.SHORT_MAX_SECONDS),
    "mid": (config.SHORT_MAX_SECONDS + 1, 600),
    "long": (601, 1800),
    "xlong": (1801, None),
}

DURATION_BAND_LABELS: dict[str, str] = {
    "short": "Short (≤ 3 min)",
    "mid": "Mid (3–10 min)",
    "long": "Long (10–30 min)",
    "xlong": "Extra long (30 min+)",
}


def _duration_band_for(seconds: int) -> str | None:
    """Return the DURATION_BANDS key a duration falls in, or None if no band
    matches (defensive — the bands cover 0..inf)."""
    for key, (lo, hi) in DURATION_BANDS.items():
        if seconds >= lo and (hi is None or seconds <= hi):
            return key
    return None


def _parse_instant(s: str | None):
    """Parse a stored timestamp (or a filter bound) to an aware UTC datetime, or
    None when absent/unparseable. Stored DB timestamps are Eastern ISO-8601 with
    offset; a date-only filter bound from a date picker is naive, so it is read as
    Eastern (config.LOCAL_TZ) before converting to UTC. Everything ends up as a
    UTC instant so comparisons are correct regardless of source offset — never a
    lexical string compare (see the timestamp-comparison invariant)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(config.LOCAL_TZ))
    return dt.astimezone(timezone.utc)


def _min_max(values: list) -> dict:
    """{'min':..,'max':..} over a list, or both None when empty."""
    if not values:
        return {"min": None, "max": None}
    return {"min": min(values), "max": max(values)}


def fetch_filter_options(
    conn: sqlite3.Connection, run_date: str, bucket: str
) -> dict:
    """Return the filter option lists and range bounds for ONE lane's ranked
    videos (run_date, bucket) — NOT global. The dashboard re-fetches these
    whenever the run or lane changes, so the rail only ever offers values present
    in the current board. Delimited matched_queries are split on '|' (the
    pipeline's delimiter); channel labels come from videos.channel_title; country
    from channels. Timestamp min/max are lexical and seed the date-picker bounds
    only — the actual window filter parses instants (see fetch_library). An empty
    lane yields empty lists / null bounds, not an error."""
    rows = conn.execute(
        """
        SELECT v.matched_queries,
               v.channel_id,
               v.channel_title,
               v.view_count,
               v.views_to_subs_ratio,
               v.published_at,
               v.first_seen_at,
               v.duration_seconds,
               c.channel_country
        FROM rankings r
        LEFT JOIN videos v ON v.video_id = r.video_id
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        WHERE r.run_date = ? AND r.bucket = ?
        """,
        (run_date, bucket),
    ).fetchall()

    matched: set[str] = set()
    channels: dict[str, str | None] = {}
    countries: set[str] = set()
    views: list[int] = []
    ratios: list[float] = []
    published: list[str] = []
    first_seen: list[str] = []
    present_bands: set[str] = set()

    for row in rows:
        mq = row["matched_queries"]
        if mq:
            matched.update(q for q in mq.split("|") if q)
        cid = row["channel_id"]
        if cid:
            title = row["channel_title"]
            # Keep the first non-empty title we see for a channel.
            if cid not in channels or (title and not channels[cid]):
                channels[cid] = title
        country = row["channel_country"]
        if country:
            countries.add(country)
        if row["view_count"] is not None:
            views.append(row["view_count"])
        if row["views_to_subs_ratio"] is not None:
            ratios.append(row["views_to_subs_ratio"])
        if row["published_at"]:
            published.append(row["published_at"])
        if row["first_seen_at"]:
            first_seen.append(row["first_seen_at"])
        if row["duration_seconds"] is not None:
            band = _duration_band_for(row["duration_seconds"])
            if band:
                present_bands.add(band)

    return {
        "matched_queries": sorted(matched),
        "channels": [
            {"channel_id": cid, "channel_title": channels[cid]}
            for cid in sorted(channels, key=lambda c: (channels[c] or "").lower())
        ],
        "countries": sorted(countries),
        "duration_bands": [
            {"key": k, "label": DURATION_BAND_LABELS[k]}
            for k in DURATION_BANDS
            if k in present_bands
        ],
        "view_count": _min_max(views),
        "views_to_subs_ratio": _min_max(ratios),
        "published_at": _min_max(published),
        "first_seen_at": _min_max(first_seen),
    }


def fetch_library(
    conn: sqlite3.Connection,
    *,
    run_date: str,
    bucket: str,
    matched_queries: list[str] | None = None,
    channel_ids: list[str] | None = None,
    countries: list[str] | None = None,
    published_after: str | None = None,
    published_before: str | None = None,
    first_seen_after: str | None = None,
    first_seen_before: str | None = None,
    view_min: int | None = None,
    view_max: int | None = None,
    ratio_min: float | None = None,
    ratio_max: float | None = None,
    duration_bands: list[str] | None = None,
    starred_only: bool = False,
    has_notes_only: bool = False,
) -> list[dict]:
    """Return one lane's ranked leaderboard for (run_date, bucket), narrowed by
    the given filters and ranked ascending. rankings is the FROM driver, so only
    ranked rows are returned (rank is always present); videos/channels are LEFT
    JOINed so a ranking whose video row is missing still returns rank/metric with
    NULL video fields (no FK enforcement). bucket is the literal lane
    ('health'/'habit'/'overall').

    Cheap predicates (numeric ranges, booleans, equality IN-lists, duration
    bands) are pushed into parametrized SQL; the delimited matched_queries filter
    and the parsed date windows are applied in Python over the small ranked set.
    A range filter excludes NULLs only when a bound is set. matched_queries uses
    split-element membership (never substring); date windows compare UTC instants
    (never lexical), dropping rows with an unparseable timestamp when a window
    bound is active. Returns plain dicts."""
    where = ["r.run_date = :run_date", "r.bucket = :bucket"]
    params: dict = {"run_date": run_date, "bucket": bucket}

    if view_min is not None:
        where.append("(v.view_count IS NOT NULL AND v.view_count >= :view_min)")
        params["view_min"] = view_min
    if view_max is not None:
        where.append("(v.view_count IS NOT NULL AND v.view_count <= :view_max)")
        params["view_max"] = view_max
    if ratio_min is not None:
        where.append(
            "(v.views_to_subs_ratio IS NOT NULL "
            "AND v.views_to_subs_ratio >= :ratio_min)"
        )
        params["ratio_min"] = ratio_min
    if ratio_max is not None:
        where.append(
            "(v.views_to_subs_ratio IS NOT NULL "
            "AND v.views_to_subs_ratio <= :ratio_max)"
        )
        params["ratio_max"] = ratio_max
    if starred_only:
        where.append("v.starred = 1")
    if has_notes_only:
        where.append("v.user_notes IS NOT NULL AND v.user_notes != ''")
    if countries:
        names = []
        for i, c in enumerate(countries):
            key = f"country_{i}"
            params[key] = c
            names.append(f":{key}")
        where.append(f"c.channel_country IN ({', '.join(names)})")
    if channel_ids:
        names = []
        for i, cid in enumerate(channel_ids):
            key = f"channel_{i}"
            params[key] = cid
            names.append(f":{key}")
        where.append(f"v.channel_id IN ({', '.join(names)})")
    if duration_bands:
        clauses = []
        for i, band in enumerate(duration_bands):
            bounds = DURATION_BANDS.get(band)
            if not bounds:
                continue
            lo, hi = bounds
            lokey = f"durlo_{i}"
            params[lokey] = lo
            if hi is None:
                clauses.append(
                    f"(v.duration_seconds IS NOT NULL "
                    f"AND v.duration_seconds >= :{lokey})"
                )
            else:
                hikey = f"durhi_{i}"
                params[hikey] = hi
                clauses.append(
                    f"(v.duration_seconds IS NOT NULL "
                    f"AND v.duration_seconds >= :{lokey} "
                    f"AND v.duration_seconds <= :{hikey})"
                )
        if clauses:
            where.append("(" + " OR ".join(clauses) + ")")

    sql = f"""
        SELECT r.rank,
               r.metric_value,
               r.video_id,
               v.title,
               v.channel_id,
               v.channel_title,
               v.link,
               v.thumbnail_url,
               v.published_at,
               v.duration_seconds,
               v.is_short,
               v.view_count,
               v.like_count,
               v.comment_count,
               v.views_to_subs_ratio,
               v.views_per_day,
               v.first_seen_at,
               v.matched_queries,
               v.starred,
               v.user_notes,
               c.channel_country,
               c.subscriber_count
        FROM rankings r
        LEFT JOIN videos v ON v.video_id = r.video_id
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        WHERE {" AND ".join(where)}
        ORDER BY r.rank ASC
    """
    rows = [dict(row) for row in conn.execute(sql, params).fetchall()]

    # Delimited matched_queries: split-element membership, never substring.
    if matched_queries:
        wanted = set(matched_queries)
        rows = [
            row for row in rows
            if wanted & {q for q in (row["matched_queries"] or "").split("|") if q}
        ]

    # Date windows: compare parsed UTC instants, dropping unparseable rows when a
    # bound is active.
    rows = _apply_window(rows, "published_at", published_after, published_before)
    rows = _apply_window(rows, "first_seen_at", first_seen_after, first_seen_before)

    return rows


def _apply_window(
    rows: list[dict], column: str, after: str | None, before: str | None
) -> list[dict]:
    """Keep rows whose `column` timestamp (parsed to a UTC instant) falls within
    [after, before] inclusive. No-op when neither bound is set. A row whose
    timestamp is absent or unparseable is dropped when any bound is active."""
    if after is None and before is None:
        return rows
    after_dt = _parse_instant(after)
    before_dt = _parse_instant(before)
    kept = []
    for row in rows:
        ts = _parse_instant(row[column])
        if ts is None:
            continue
        if after_dt is not None and ts < after_dt:
            continue
        if before_dt is not None and ts > before_dt:
            continue
        kept.append(row)
    return kept


# --- Phase 2: change-over-time read helpers ---------------------------------


def fetch_snapshot_series(
    conn: sqlite3.Connection, video_ids: list[str]
) -> dict[str, dict]:
    """Return per-video stats_snapshots time-series, keyed by video_id, each a dict
    {"title": <videos.title or None>, "points": [{captured_at, view_count,
    like_count, comment_count}, ...]} with points ascending by captured_at. The
    title comes from a LEFT JOIN on videos (charts label series by title); a video
    row always exists upstream when snapshots exist. The snapshot grain is one row
    per (run_id, video_id), so a video seen once yields a single-point series and a
    never-snapshotted video is simply absent from the result (the caller
    normalizes). Snapshots are NOT lane-scoped. Empty video_ids returns {}."""
    if not video_ids:
        return {}
    names = []
    params: dict = {}
    for i, vid in enumerate(video_ids):
        key = f"vid_{i}"
        params[key] = vid
        names.append(f":{key}")
    rows = conn.execute(
        f"""
        SELECT s.video_id,
               v.title,
               s.captured_at,
               s.view_count,
               s.like_count,
               s.comment_count
        FROM stats_snapshots s
        LEFT JOIN videos v ON v.video_id = s.video_id
        WHERE s.video_id IN ({', '.join(names)})
        ORDER BY s.video_id, s.captured_at ASC
        """,
        params,
    ).fetchall()

    series: dict[str, dict] = {}
    for row in rows:
        entry = series.setdefault(
            row["video_id"], {"title": row["title"], "points": []}
        )
        entry["points"].append(
            {
                "captured_at": row["captured_at"],
                "view_count": row["view_count"],
                "like_count": row["like_count"],
                "comment_count": row["comment_count"],
            }
        )
    return series


def _velocity_points(series: list[dict]) -> list[dict]:
    """Views-per-day velocity between consecutive snapshots. captured_at is parsed
    to a UTC instant via _parse_instant (the same helper the date-window filter
    uses — never a lexical compare). A non-positive time delta (clock skew or
    same-instant snapshots) is skipped so the division is always safe, mirroring
    how the pipeline guards its metric math. A single-point (or empty) series has
    no pairs and yields []."""
    out: list[dict] = []
    for prev, cur in zip(series, series[1:]):
        t0 = _parse_instant(prev["captured_at"])
        t1 = _parse_instant(cur["captured_at"])
        if t0 is None or t1 is None:
            continue
        delta = (t1 - t0).total_seconds()
        if delta <= 0:
            continue
        if prev["view_count"] is None or cur["view_count"] is None:
            continue
        views_per_day = (cur["view_count"] - prev["view_count"]) / delta * 86400
        out.append(
            {"captured_at": cur["captured_at"], "views_per_day": views_per_day}
        )
    return out


def fetch_rank_history(
    conn: sqlite3.Connection, bucket: str, video_ids: list[str] | None = None
) -> dict:
    """Return one lane's rank movement across ALL run_dates (the bump chart and
    the views_to_subs_ratio-over-time series share this single source). bucket is
    a single value (plain equality, like fetch_lane); videos are LEFT JOINed so a
    ranking whose video row is missing still appears. Optionally restrict to
    video_ids (the tracked-video trajectory). There is intentionally NO run_date
    bound: a bump chart's whole job is movement across runs.

    Returns {bucket, run_dates: [distinct ascending], series: [{video_id, title,
    channel_title, link, thumbnail_url, points: [{run_date, rank, metric_value}]}]}.
    A video absent on a run_date simply has no point for it (the 'fell off' gap).
    metric_value is the views_to_subs_ratio at that run — lane-scoped and sparse,
    present only for runs where the video was ranked in this lane (stats_snapshots
    stores no subscriber_count, so the ratio cannot be sourced there)."""
    where = ["r.bucket = :bucket"]
    params: dict = {"bucket": bucket}
    if video_ids:
        names = []
        for i, vid in enumerate(video_ids):
            key = f"vid_{i}"
            params[key] = vid
            names.append(f":{key}")
        where.append(f"r.video_id IN ({', '.join(names)})")

    rows = conn.execute(
        f"""
        SELECT r.run_date,
               r.rank,
               r.video_id,
               r.metric_value,
               v.title,
               v.channel_title,
               v.link,
               v.thumbnail_url
        FROM rankings r
        LEFT JOIN videos v ON v.video_id = r.video_id
        WHERE {" AND ".join(where)}
        ORDER BY r.run_date ASC, r.rank ASC
        """,
        params,
    ).fetchall()

    run_dates: list[str] = []
    seen_dates: set[str] = set()
    series: dict[str, dict] = {}
    for row in rows:
        rd = row["run_date"]
        if rd not in seen_dates:
            seen_dates.add(rd)
            run_dates.append(rd)
        vid = row["video_id"]
        entry = series.get(vid)
        if entry is None:
            entry = {
                "video_id": vid,
                "title": row["title"],
                "channel_title": row["channel_title"],
                "link": row["link"],
                "thumbnail_url": row["thumbnail_url"],
                "points": [],
            }
            series[vid] = entry
        entry["points"].append(
            {
                "run_date": rd,
                "rank": row["rank"],
                "metric_value": row["metric_value"],
            }
        )

    return {
        "bucket": bucket,
        "run_dates": run_dates,
        "series": list(series.values()),
    }
