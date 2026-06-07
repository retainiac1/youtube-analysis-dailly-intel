import sqlite3
import time
from contextlib import contextmanager

# Schema version stamped into PRAGMA user_version. Bump this and branch in
# init_db when a future, non-destructive migration is needed.
SCHEMA_VERSION = 1

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
        saves TEXT DEFAULT ''
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
]


def get_connection(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL journaling and foreign keys enabled and
    a row factory that yields name-addressable rows. The caller is responsible
    for closing the connection."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str) -> None:
    """Create all tables if they do not exist and stamp the schema version.

    Safe to run repeatedly: every statement uses CREATE TABLE IF NOT EXISTS and
    PRAGMA user_version is set idempotently. The user_version read is the hook
    for future, non-destructive migrations."""
    conn = get_connection(db_path)
    try:
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        # Future migrations branch on current_version here. For v1 the
        # IF NOT EXISTS statements are sufficient for both fresh and existing DBs.
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        if current_version != SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
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
    sql = _build_upsert_sql(
        "videos", "video_id", VIDEO_API_COLUMNS,
        ["first_seen_at", "last_api_refresh_at"], refresh_clause,
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
