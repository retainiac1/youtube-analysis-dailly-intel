import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# User-tunable settings (externalized to settings.toml)
#
# These knobs live in a hand-editable TOML file at the repo root, NOT in the DB,
# so a future DB reset/wipe can't destroy them and they stay git-diffable. They
# are loaded at import below and exposed under the same names this module always
# used, so nothing downstream changes its imports. Each tunable has a DEFAULT_*
# fallback here, so a missing file or a missing key degrades safely instead of
# crashing. Validation (validate_config) runs over the loaded values at import —
# an external file means external typos, so it is load-bearing.
# ---------------------------------------------------------------------------
DEFAULT_MIN_VIEWS = 10_000
DEFAULT_WINDOW_DAYS = 3
DEFAULT_TOP_N = 20
DEFAULT_SHORT_MAX_SECONDS = 180
DEFAULT_SEARCH_RELEVANCE_LANGUAGE = "en"
DEFAULT_DAILY_QUOTA_LIMIT = 10000
DEFAULT_SAFETY_BUFFER = 500
# ISO-3166 region whose category titles seed the `categories` table. Category IDs
# are effectively global, so any English region yields the same titles.
DEFAULT_CATEGORY_REGION = "US"
DEFAULT_SEARCH_QUERIES = [
    {"q": "build habits", "bucket": "habit"},
    {"q": "break bad habits", "bucket": "habit"},
    {"q": "habit tracker", "bucket": "habit"},
    {"q": "atomic habits", "bucket": "habit"},
    {"q": "habit stacking", "bucket": "habit"},
    {"q": "morning routine habits", "bucket": "habit"},
    {"q": "zone 2 cardio", "bucket": "health"},
    {"q": "VO2 max", "bucket": "health"},
    {"q": "strength training over 50", "bucket": "health"},
    {"q": "high protein", "bucket": "health"},
    {"q": "longevity habits", "bucket": "health"},
    {"q": "sleep routine", "bucket": "health"},
]

# Display-time cost map for the interpretation generator, keyed by the canonical
# "provider:model" string. Each value is USD per 1M tokens. Estimates only (no
# provider returns dollar cost, and stored figures rot when prices change); used
# at display time to label spend an estimate. Verified 2026-06-09; will drift.
DEFAULT_PRICES = {
    "anthropic:claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "openai:gpt-5.4-nano": {"input": 0.20, "output": 1.25},
    "xai:grok-4-fast": {"input": 0.20, "output": 0.50},
    "google:gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
}

# name -> default, for every externalized tunable. load_settings() merges the
# parsed TOML over these, so a missing key always resolves to its default.
SETTINGS_DEFAULTS: dict[str, object] = {
    "MIN_VIEWS": DEFAULT_MIN_VIEWS,
    "WINDOW_DAYS": DEFAULT_WINDOW_DAYS,
    "TOP_N": DEFAULT_TOP_N,
    "SHORT_MAX_SECONDS": DEFAULT_SHORT_MAX_SECONDS,
    "SEARCH_RELEVANCE_LANGUAGE": DEFAULT_SEARCH_RELEVANCE_LANGUAGE,
    "DAILY_QUOTA_LIMIT": DEFAULT_DAILY_QUOTA_LIMIT,
    "SAFETY_BUFFER": DEFAULT_SAFETY_BUFFER,
    "CATEGORY_REGION": DEFAULT_CATEGORY_REGION,
    "SEARCH_QUERIES": DEFAULT_SEARCH_QUERIES,
    "PRICES": DEFAULT_PRICES,
}

# Resolve relative to THIS file, not CWD, so it works regardless of where the
# script is launched from.
_SETTINGS_PATH = Path(__file__).resolve().with_name("settings.toml")


def load_settings(path: Path | str = _SETTINGS_PATH) -> dict:
    """Load the user-tunable settings from a TOML file, merged over the in-code
    defaults so the result always has every key.

    A missing file is non-fatal: it prints one stderr warning and falls back to
    all defaults (a fresh checkout still runs). A missing individual key falls
    back to its DEFAULT_*. The merge happens unconditionally after the read, so
    every return value carries the full key set — callers can index any tunable
    without a KeyError. The values are NOT validated here; run validate_config
    over the loaded module to reject external typos."""
    try:
        with open(path, "rb") as f:
            parsed = tomllib.load(f)
    except FileNotFoundError:
        print(
            f"warning: settings file not found at {path}; using built-in defaults",
            file=sys.stderr,
        )
        parsed = {}
    return {name: parsed.get(name, default) for name, default in SETTINGS_DEFAULTS.items()}


_settings = load_settings()
MIN_VIEWS = _settings["MIN_VIEWS"]
WINDOW_DAYS = _settings["WINDOW_DAYS"]
TOP_N = _settings["TOP_N"]
SHORT_MAX_SECONDS = _settings["SHORT_MAX_SECONDS"]
SEARCH_RELEVANCE_LANGUAGE = _settings["SEARCH_RELEVANCE_LANGUAGE"]
DAILY_QUOTA_LIMIT = _settings["DAILY_QUOTA_LIMIT"]
SAFETY_BUFFER = _settings["SAFETY_BUFFER"]
CATEGORY_REGION = _settings["CATEGORY_REGION"]
SEARCH_QUERIES = _settings["SEARCH_QUERIES"]
PRICES = _settings["PRICES"]

PUBLISHED_AFTER = "2025-09-01T00:00:00Z"
PUBLISHED_BEFORE = None

# Persistent SQLite pipeline (Phase 1). PUBLISHED_AFTER is retained for the
# existing pipeline; get_published_after() is the forward-looking helper and is
# intentionally not wired into swipefile.py yet.
DB_PATH = "data/database/swipefile.db"
QUOTA_RESET_TZ = "America/Los_Angeles"

# All stored DB timestamps are Eastern, ISO-8601 with offset (never UTC, never
# naive) — see now_local_iso(). The YouTube API param in get_published_after()
# is the one exemption (it must be UTC/Z).
LOCAL_TZ = "America/New_York"

VALID_BUCKETS = {"health", "habit"}

# NOTE: MIN_VIEWS, SHORT_MAX_SECONDS and SEARCH_RELEVANCE_LANGUAGE are
# user-tunable and now loaded from settings.toml above (see SETTINGS_DEFAULTS).
# MIN_VIEWS is the live-tuned qualifying-view floor; SHORT_MAX_SECONDS remains
# the single source of truth for the Shorts threshold (both filter_videos and
# the is_short derivation read it, so they can never disagree).
RESULTS_PER_QUERY = 50
OUTPUT_CSV = "youtube_habits_swipefile.csv"
OUTPUT_XLSX_BASE = "youtube_habits_swipefile"
STATE_FILE = "state.json"

SEARCH_QUOTA_COST = 100
VIDEOS_QUOTA_COST = 1
CHANNELS_QUOTA_COST = 1
CATEGORIES_QUOTA_COST = 1
COMMENTS_QUOTA_COST = 1
CHANNEL_BATCH_SIZE = 50
COMMENTS_PER_VIDEO = 10
BLOCKED_CATEGORY_IDS = {"1", "10", "24"}

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
    "SEARCH_RELEVANCE_LANGUAGE": str,
    "CATEGORY_REGION": str,
    "SEARCH_QUERIES": list,
    "PRICES": dict,
}

# Keys that must be strictly positive ints (type is checked via REQUIRED_KEYS).
POSITIVE_INT_KEYS: frozenset[str] = frozenset(
    {"MIN_VIEWS", "WINDOW_DAYS", "TOP_N", "SHORT_MAX_SECONDS",
     "DAILY_QUOTA_LIMIT", "SAFETY_BUFFER"}
)


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


def pacific_date(eastern_iso: str | None = None) -> str:
    """Derive the Pacific (QUOTA_RESET_TZ) calendar date 'YYYY-MM-DD' from an
    Eastern, offset-bearing timestamp (defaults to now_local_iso()).

    This is the project's ONE Eastern->Pacific conversion. It exists only because
    Google's API quota resets at midnight Pacific, so quota_ledger is keyed by the
    Pacific date. The date is always derived by code from a stored Eastern
    timestamp, never hand-set. The input must carry an offset (it does, coming
    from now_local_iso() or a DB timestamp), so the conversion is unambiguous;
    we parse to an aware datetime and astimezone — never compare strings."""
    src = eastern_iso or now_local_iso()
    dt = datetime.fromisoformat(src)
    return dt.astimezone(ZoneInfo(QUOTA_RESET_TZ)).date().isoformat()


def validate_config(cfg: object | None = None) -> None:
    """Validate that required config keys are present, correctly typed, and
    in-range, and that every SEARCH_QUERIES entry has a non-empty `q` and a valid
    `bucket`.

    Now that the tunables come from an external settings.toml, this is load-bearing
    against hand-edit typos: it checks positivity for the int knobs, that
    SAFETY_BUFFER < DAILY_QUOTA_LIMIT, and that SEARCH_QUERIES is non-empty.

    Raises ConfigError naming the offending key on the first failure. `cfg`
    defaults to this module; pass any object exposing the keys as attributes
    (e.g. a SimpleNamespace) to validate an alternate configuration."""
    if cfg is None:
        cfg = sys.modules[__name__]

    for key, expected_type in REQUIRED_KEYS.items():
        if not hasattr(cfg, key):
            raise ConfigError(f"Missing required config key: {key}")
        value = getattr(cfg, key)
        # bool is a subclass of int; reject it where a plain int is required.
        # (A hand-edited `TOP_N = true` in TOML parses to a Python bool — this
        # is exactly the typo the externalized-file validation must catch.)
        if not isinstance(value, expected_type) or (
            expected_type is int and isinstance(value, bool)
        ):
            raise ConfigError(
                f"Config key {key} must be {expected_type.__name__}, "
                f"got {type(value).__name__}"
            )

    for key in POSITIVE_INT_KEYS:
        if getattr(cfg, key) <= 0:
            raise ConfigError(f"Config key {key} must be a positive int")

    if getattr(cfg, "SAFETY_BUFFER") >= getattr(cfg, "DAILY_QUOTA_LIMIT"):
        raise ConfigError("Config key SAFETY_BUFFER must be < DAILY_QUOTA_LIMIT")

    # CATEGORY_REGION feeds the videoCategories.list regionCode; an empty string
    # would silently break the fetch, so reject it even though it types as str.
    if not getattr(cfg, "CATEGORY_REGION").strip():
        raise ConfigError("Config key CATEGORY_REGION must be a non-empty string")

    queries = getattr(cfg, "SEARCH_QUERIES")
    if not queries:
        raise ConfigError("Config key SEARCH_QUERIES must be a non-empty list")

    for i, entry in enumerate(queries):
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

    # PRICES: a display-time cost map keyed by the canonical "provider:model".
    # Each entry must carry numeric, non-negative input and output rates. A
    # malformed hand-edit fails loud here, naming the offending model key.
    prices = getattr(cfg, "PRICES")
    for key, entry in prices.items():
        where = f"PRICES[{key!r}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be a dict, got {type(entry).__name__}")
        for field in ("input", "output"):
            value = entry.get(field)
            # bool is an int subclass; reject it where a number is required.
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ConfigError(
                    f"{where} '{field}' must be a number, got {value!r}"
                )
            if value < 0:
                raise ConfigError(f"{where} '{field}' must be non-negative, got {value}")


# Validate the loaded settings at import. An external settings.toml means
# external typos, so this is load-bearing: a malformed file fails loudly and
# immediately with a named ConfigError rather than corrupting a run downstream.
validate_config()
