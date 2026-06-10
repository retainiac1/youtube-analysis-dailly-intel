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
SCHEMA_VERSION = 4

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
        generated_at TEXT
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


def init_db(db_path: str) -> None:
    """Create all tables if they do not exist and stamp the schema version.

    Safe to run repeatedly: every statement uses CREATE TABLE IF NOT EXISTS and
    PRAGMA user_version is set idempotently. The user_version read is the hook
    for future, non-destructive migrations."""
    # SQLite will not create missing parent directories; ensure they exist.
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
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
                          text: str, model: str, now: str) -> None:
    """Insert or overwrite the single interpretation for (run_date, scope). On
    conflict the text, model, and generated_at are replaced (re-running a lane
    overwrites its summary). `model` is the canonical "provider:model" string.
    Composite-PK shape, so the SQL is written inline rather than via
    _build_upsert_sql (that helper is shaped for single-key tables). Does not
    commit — the caller wraps it in `transaction`."""
    conn.execute(
        """
        INSERT INTO interpretations (run_date, scope, text, model, generated_at)
        VALUES (:run_date, :scope, :text, :model, :now)
        ON CONFLICT(run_date, scope) DO UPDATE SET
            text = excluded.text,
            model = excluded.model,
            generated_at = excluded.generated_at
        """,
        {"run_date": run_date, "scope": scope, "text": text,
         "model": model, "now": now},
    )


def log_invocation(conn: sqlite3.Connection, run_date: str, scope: str,
                   model: str, temperature: float, seed, filter,
                   input_tokens: int, output_tokens: int, now: str) -> None:
    """Append one row to the llm_invocations log recording the full parameter set
    that produced a generation. Append-only (autoincrement id), so re-running a
    lane adds a new row rather than overwriting. `model` is canonical
    "provider:model"; `seed` is NULL when the provider did not apply one; `filter`
    is NULL in v1. Does not commit — the caller wraps it in `transaction`."""
    conn.execute(
        """
        INSERT INTO llm_invocations
            (run_date, scope, model, temperature, seed, filter,
             input_tokens, output_tokens, generated_at)
        VALUES (:run_date, :scope, :model, :temperature, :seed, :filter,
                :input_tokens, :output_tokens, :now)
        """,
        {"run_date": run_date, "scope": scope, "model": model,
         "temperature": temperature, "seed": seed, "filter": filter,
         "input_tokens": input_tokens, "output_tokens": output_tokens,
         "now": now},
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
               v.views_to_subs_ratio
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
    """Return the interpretations row for (run_date, scope) or None when absent.
    scope holds a bucket value (health / habit / overall). Read-only; the row is
    written by an external generator (upsert_interpretation lands in Phase 4)."""
    return conn.execute(
        "SELECT run_date, scope, text, model, generated_at "
        "FROM interpretations WHERE run_date = ? AND scope = ?",
        (run_date, scope),
    ).fetchone()


# --- Refresh support --------------------------------------------------------

def fetch_videos_for_refresh(conn: sqlite3.Connection) -> list[dict]:
    """Return, for every tracked video, the fields a --refresh must PRESERVE
    (videos.list will not re-supply them): video_id, channel_id, matched_queries,
    buckets, top_comments. Stats are re-fetched from the API, not read here."""
    cur = conn.execute(
        "SELECT video_id, channel_id, matched_queries, buckets, top_comments "
        "FROM videos"
    )
    return [dict(row) for row in cur.fetchall()]


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
