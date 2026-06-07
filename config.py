from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SEARCH_QUERIES = [
    {"q": "build habits", "bucket": "habit"},
    {"q": "break bad habits", "bucket": "habit"},
    {"q": "habit tracker", "bucket": "habit"},
    {"q": "atomic habits", "bucket": "habit"},
    {"q": "habit stacking", "bucket": "habit"},
    {"q": "morning routine habits", "bucket": "habit"},
]

PUBLISHED_AFTER = "2025-09-01T00:00:00Z"
PUBLISHED_BEFORE = None

# Persistent SQLite pipeline (Phase 1). PUBLISHED_AFTER is retained for the
# existing pipeline; get_published_after() is the forward-looking helper and is
# intentionally not wired into swipefile.py yet.
DB_PATH = "swipefile.db"
WINDOW_DAYS = 3
TOP_N = 20
DAILY_QUOTA_LIMIT = 10000
SAFETY_BUFFER = 500
QUOTA_RESET_TZ = "America/Los_Angeles"

# All stored DB timestamps are Eastern, ISO-8601 with offset (never UTC, never
# naive) — see now_local_iso(). The YouTube API param in get_published_after()
# is the one exemption (it must be UTC/Z).
LOCAL_TZ = "America/New_York"

VALID_BUCKETS = {"health", "habit"}

MIN_VIEWS = 100_000
# Single source of truth for the Shorts duration threshold: both filter_videos
# and the is_short derivation read this, so they can never disagree.
SHORT_MAX_SECONDS = 180
RESULTS_PER_QUERY = 50
OUTPUT_CSV = "youtube_habits_swipefile.csv"
OUTPUT_XLSX_BASE = "youtube_habits_swipefile"
STATE_FILE = "state.json"

SEARCH_QUOTA_COST = 100
VIDEOS_QUOTA_COST = 1
CHANNELS_QUOTA_COST = 1
COMMENTS_QUOTA_COST = 1
CHANNEL_BATCH_SIZE = 50
COMMENTS_PER_VIDEO = 10
BLOCKED_CATEGORY_IDS = {"1", "10", "24"}
SEARCH_RELEVANCE_LANGUAGE = "en"

CSV_COLUMNS = [
    "social_media",
    "hook",
    "first_10_sec",
    "format",
    "date",
    "likes",
    "saves",
    "views",
    "thumbnail",
    "link",
    "title",
    "description",
    "channel",
    "channel_id",
    "subscriber_count",
    "channel_video_count",
    "channel_total_views",
    "channel_created_date",
    "channel_country",
    "channel_keywords",
    "views_to_subs_ratio",
    "category_id",
    "audio_language",
    "definition",
    "has_captions",
    "made_for_kids",
    "tags",
    "topic_categories",
    "top_comments",
    "duration_seconds",
    "matched_queries",
]

COMMENTS_SHEET_COLUMNS = [
    "video_id",
    "video_title",
    "comment_rank",
    "author",
    "text",
    "likes",
]


# Required scalar config keys and their expected types, used by validate_config.
REQUIRED_KEYS: dict[str, type] = {
    "DB_PATH": str,
    "WINDOW_DAYS": int,
    "TOP_N": int,
    "DAILY_QUOTA_LIMIT": int,
    "SAFETY_BUFFER": int,
    "QUOTA_RESET_TZ": str,
    "LOCAL_TZ": str,
    "MIN_VIEWS": int,
    "SHORT_MAX_SECONDS": int,
    "SEARCH_QUERIES": list,
}


class ConfigError(ValueError):
    """Raised when configuration is missing a key, has a wrong type, or holds an
    invalid SEARCH_QUERIES entry. Subclasses ValueError so callers can catch
    either."""


def get_published_after() -> str:
    """Return the RFC3339-Z timestamp for now minus WINDOW_DAYS, matching the
    format of the existing PUBLISHED_AFTER constant. Defined for later phases;
    not wired into swipefile.py yet.

    NOTE: this is a YouTube API *request parameter* and must stay UTC/Z — it is
    the one exemption to the Eastern-only DB-timestamp rule (see now_local_iso)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_local_iso() -> str:
    """Return the current time as an Eastern (LOCAL_TZ) ISO-8601 string with
    offset, e.g. '2026-06-07T14:30:00-04:00'. This is the canonical source for
    every timestamp written to the DB — Eastern, offset-bearing (DST-correct via
    zoneinfo), never UTC, never naive."""
    return datetime.now(ZoneInfo(LOCAL_TZ)).isoformat(timespec="seconds")


def validate_config(cfg: object | None = None) -> None:
    """Validate that required config keys are present and correctly typed and
    that every SEARCH_QUERIES entry has a non-empty `q` and a valid `bucket`.

    Raises ConfigError naming the offending key on the first failure. `cfg`
    defaults to this module; pass any object exposing the keys as attributes
    (e.g. a SimpleNamespace) to validate an alternate configuration."""
    if cfg is None:
        import sys
        cfg = sys.modules[__name__]

    for key, expected_type in REQUIRED_KEYS.items():
        if not hasattr(cfg, key):
            raise ConfigError(f"Missing required config key: {key}")
        value = getattr(cfg, key)
        # bool is a subclass of int; reject it where a plain int is required.
        if not isinstance(value, expected_type) or (
            expected_type is int and isinstance(value, bool)
        ):
            raise ConfigError(
                f"Config key {key} must be {expected_type.__name__}, "
                f"got {type(value).__name__}"
            )

    for i, entry in enumerate(getattr(cfg, "SEARCH_QUERIES")):
        where = f"SEARCH_QUERIES[{i}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be a dict, got {type(entry).__name__}")
        q = entry.get("q")
        if not isinstance(q, str) or not q.strip():
            raise ConfigError(f"{where} has an empty or non-string 'q'")
        bucket = entry.get("bucket")
        if bucket not in VALID_BUCKETS:
            allowed = ", ".join(sorted(VALID_BUCKETS))
            raise ConfigError(
                f"{where} has invalid bucket {bucket!r}; must be one of {allowed}"
            )
