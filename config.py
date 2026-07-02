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
# Max days a tracked video may go without view-count growth before the daily
# catalog sweep retires it (status -> aged_out). The discovery window is only
# WINDOW_DAYS, so after that a video is reachable solely by the sweep; 30 days
# covers dwell + drop-off + the decay tail we want to keep snapshotting. Starred
# videos are exempt. Positive int; do not go below 21.
DEFAULT_REFRESH_MAX_AGE_DAYS = 30
DEFAULT_TOP_N = 20
# Cap on how many videos each Lifecycle dashboard line cohort draws. Two DISJOINT
# cohorts share this single size: the growth / ratio charts take the top N by view
# velocity (fastest views/day), the rank-drift bump takes the top N by best rank in
# the window. Kept small so the legend-less bump and the scroll-legend line charts
# stay legible. Positive int.
DEFAULT_LIFECYCLE_COHORT_SIZE = 6
# Size of the Lifecycle growth cohort (View growth, Views-per-day, Velocity bars):
# the top N videos by PEAK views/day over their tracked life. Larger than the bump
# cohort so the curves reach back across the window (the all-time peak growers
# include long-tracked videos, not just this week's surge). Positive int.
DEFAULT_LIFECYCLE_GROWTH_COHORT_SIZE = 20
DEFAULT_SHORT_MAX_SECONDS = 180
DEFAULT_SEARCH_RELEVANCE_LANGUAGE = "en"
DEFAULT_DAILY_QUOTA_LIMIT = 10000
DEFAULT_SAFETY_BUFFER = 500
# Local Ollama HTTP endpoint the adapter calls. Loopback only (never 0.0.0.0): the
# model server is a local single-user process. No URL literal lives in the adapter.
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
# Per-request timeout (seconds) for the Ollama call. A 5-minute fail-fast ceiling:
# a local run can be slow, but a not-running/hung/grinding server should surface a
# clean error in minutes, not block the request for ~10. Overridable in settings.toml
# for genuinely slower local models.
DEFAULT_OLLAMA_TIMEOUT_SECONDS = 300
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

# SEED literal for the model_prices table, keyed by the canonical "provider:model"
# string; each value is USD per 1M tokens. As of v7 this is NO LONGER read at
# display time — db.init_db's offline seed copies these into model_prices, and spend
# prices each invocation against its effective window there. Kept here only as the
# seed source (consumed by _seed_models / seed_models.py) and validate_config's
# subject. Verified 2026-06-09; the DB windows are now the live source of truth.
DEFAULT_PRICES = {
    "anthropic:claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "openai:gpt-5.4-nano": {"input": 0.20, "output": 1.25},
    "xai:grok-4.3": {"input": 1.25, "output": 2.50},
    "google:gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
    # Local Ollama inference is free: a $0/$0 window so spend prices it to exactly 0.
    "ollama:qwen3.5:9b": {"input": 0.0, "output": 0.0},
}

# The baseline model registry that db.init_db offline-seeds into the `models`
# table (insert-if-empty) so a freshly-migrated DB is never broken: a populated
# dropdown, working validation, priceable spend. The capability flags are captured
# here as DATA — the one-time snapshot of the old name-pattern logic that the
# tables replace: Anthropic's Messages API has no seed (supports_seed=0); the
# gpt-5 reasoning model rejects a non-default temperature (supports_temperature=0)
# and is flagged is_reasoning; xAI and Gemini honor both. Prices are NOT duplicated
# here: the seed reads them from DEFAULT_PRICES so prices keep one home. The xAI model
# is xai:grok-4.3, live-verified on docs.x.ai (Reasoning: Configurable, so is_reasoning=1;
# $1.25/$2.50 per MTok).
SEED_MODELS = [
    {"model": "anthropic:claude-haiku-4-5", "provider": "anthropic",
     "supports_temperature": 1, "supports_seed": 0, "is_reasoning": 0},
    {"model": "openai:gpt-5.4-nano", "provider": "openai",
     "supports_temperature": 0, "supports_seed": 1, "is_reasoning": 1},
    {"model": "xai:grok-4.3", "provider": "xai",
     "supports_temperature": 1, "supports_seed": 1, "is_reasoning": 1},
    {"model": "google:gemini-2.5-flash-lite", "provider": "google",
     "supports_temperature": 1, "supports_seed": 1, "is_reasoning": 0},
    # Local Ollama thinking model. Phase 0/Step 0 verified live: temperature and
    # seed are honored (seed proven byte-identical on a high-entropy run), and the
    # model advertises a `thinking` capability (is_reasoning=1 drives the per-run
    # think toggle). Provider `ollama` honors think; see llm.THINK_PROVIDERS.
    {"model": "ollama:qwen3.5:9b", "provider": "ollama",
     "supports_temperature": 1, "supports_seed": 1, "is_reasoning": 1},
]

# Colors, thresholds, and donut radii for the dashboard's LLM-spend visual (the
# spend-share donut + token/cost leaderboard). Kept here, not hardcoded in JS/CSS,
# so the palette is tunable in one place and validated at startup. The bar mapping
# is fixed: tokens = blue, dollars = green. The badge palette is a SINGLE GREEN
# RAMP by efficiency (NOT a red/green stoplight): efficient (low $/M) = deepest
# green, expensive (high $/M) = brightest green, mid in between. Each fill carries
# a contrasting text color so the pill reads on the dark glass panel.
DEFAULT_SPEND_VIZ = {
    "token_bar_color": "#2D7CFF",  # brand --accent-blue
    "cost_bar_color": "#00F2A9",   # brand --accent-cyan (mint)
    # A mint ramp, BRIGHTEST first; slice i (by share, largest first) gets color i,
    # wrapping if there are more models than colors. Largest share => most saturated
    # / most visible, dimming toward a muted teal-gray for the tail.
    "donut_slice_colors": [
        "#00F2A9", "#22D3A4", "#3BB89B", "#4A9C8E", "#527F7E", "#55636B",
    ],
    "donut_inner_radius": "58%",
    "donut_outer_radius": "80%",
    "efficiency_good": 0.50,
    "efficiency_warn": 1.00,
    "good_bg": "#14532D",
    "good_fg": "#D1FAE5",
    "mid_bg": "#15803D",
    "mid_fg": "#ECFDF5",
    "warn_bg": "#4ADE80",
    "warn_fg": "#052E16",
}

# Validation thresholds for the price-refresh agent (per-MTok magnitude band,
# cross-source agreement tolerance, and the auto-apply vs stage-for-review delta
# boundary). The agent's pure validation layers take these as explicit params (no
# hardcoded defaults in their signatures); this dict is the one place the values
# live, validated at startup like the rest. magnitude_lo < magnitude_hi; cross_tol
# and delta_threshold are fractions in (0, 1).
DEFAULT_PRICE_REFRESH = {
    "magnitude_lo": 0.02,    # per-MTok floor (a value below this is a unit error)
    "magnitude_hi": 200.0,   # per-MTok ceiling
    "cross_tol": 0.05,       # cross-source agreement tolerance (fraction)
    "delta_threshold": 0.10, # auto-apply (<) vs stage-for-review (>=) boundary
    # User-Agent the fetcher sends. Some provider pricing pages 403 a default httpx
    # client; a browser UA clears them. Lives here (config), never a literal in the
    # fetcher, so it is tunable without a code change.
    "user_agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
}

# The model that powers price extraction (canonical provider:model). Reads pricing
# page text and emits strict JSON; any capable registry model works. A fresh-checkout
# fallback — settings.toml overrides it.
DEFAULT_EXTRACTION_MODEL = "anthropic:claude-haiku-4-5"

# LLM gate that enforces English-only + drops kids content on each discovered video
# (a binary classification the YouTube API cannot express). ENABLED toggles the phase
# off without code changes. MODEL is the primary classifier (cheapest non-local), with
# FALLBACK_MODEL a DIFFERENT-provider backup used when the primary provider is
# unreachable (validate_config enforces the different-provider rule). Fresh-checkout
# fallbacks; settings.toml overrides them. The actual model in effect resolves through
# classify.active_classification_model (app_preferences override -> this default), so a
# future manual-run UI can change it without editing the file.
DEFAULT_CLASSIFICATION_ENABLED = True
DEFAULT_CLASSIFICATION_MODEL = "google:gemini-2.5-flash-lite"
DEFAULT_CLASSIFICATION_FALLBACK_MODEL = "anthropic:claude-haiku-4-5"

# The classify lane's resilience policy when the primary (Gemini) is throttled
# (429 RESOURCE_EXHAUSTED) or overloaded (503 UNAVAILABLE). One coupled dict (the
# four values move together as a retry/breaker policy), so they live here, never
# inline magic numbers. max_retries is the EXTRA attempts after the first call, so
# 1 yields 2 attempts (initial + 1 retry) per model per video; a 429 honors the
# response retryDelay (capped at max_backoff_seconds) else exponential backoff from
# base_backoff_seconds; 503 uses exponential backoff. max_fallback_videos is the
# per-run spend breaker: once this many distinct videos have been routed to the
# paid fallback, the next would-be fallback aborts the run (EXIT_CLASSIFY_BUDGET)
# rather than spend further. Fresh-checkout fallback; settings.toml overrides it.
DEFAULT_CLASSIFICATION_RETRY = {
    "max_retries": 1,
    "base_backoff_seconds": 1.0,
    "max_backoff_seconds": 30.0,
    "max_fallback_videos": 150,
}

# Deterministic title-keyword reject list for the cheap gate (_classify_video): drops
# obvious kid/parenting videos by title BEFORE the LLM classify phase, cutting LLM
# volume and catching obvious cases the LLM has let through. Matched whole-word,
# case-insensitive; a match is a PERMANENT drop (no LLM safety net), so the list is
# high-precision (no terms that appear in the adult longevity niche, e.g. NOT
# "bedtime"). Tunable in settings.toml; the owner curates it. Fresh-checkout fallback.
DEFAULT_KID_TITLE_KEYWORDS = [
    "baby", "babies", "infant", "newborn", "toddler", "toddlers", "kid", "kids",
    "kiddo", "child", "children", "nursery", "preschool", "kindergarten", "daycare",
    "sahm", "momlife", "girlmom", "boymom", "parenting", "for kids", "for toddlers",
    "cocomelon", "paw patrol", "peppa", "bluey", "baby shark", "nursery rhyme",
    "lullaby",
]

# ONE official, server-rendered pricing page per non-local provider (the fetcher runs
# no JS). The scrape is the value-of-record; it is cross-checked against a third-party
# validator feed (PRICE_VALIDATION), not a second scrape. Fresh-checkout fallback;
# settings.toml overrides it. Live-verified SSR URLs (the openai marketing page and the
# xai cards page are JS-rendered, so the docs URLs are used instead).
DEFAULT_PRICE_SOURCES = {
    "anthropic": "https://www.anthropic.com/pricing",
    "openai": "https://platform.openai.com/docs/pricing",
    "xai": "https://docs.x.ai/docs/models",
    "google": "https://ai.google.dev/pricing",
}

# Third-party validator feeds that cross-check the scraped price (a SECOND, independent
# opinion, not a scrape mirror). LiteLLM is tried first, OpenRouter is the fetch-failure
# fallback. tolerance_pct = the match band; review_band_pct = the drift-vs-mismatch
# boundary (tolerance strictly tighter than review_band). model_keys maps each canonical
# 'provider:model' to its NON-byte-equal id in each feed (explicit map, never a transform
# heuristic): LiteLLM mostly uses the bare model_id but prefixes grok with 'xai/';
# OpenRouter uses its own 'vendor/model' ids (dot in claude-haiku-4.5, 'x-ai' for grok).
# The *_display_url values are the human-friendly link targets (raw endpoints render as
# raw JSON); the page links to these while the fetcher reads the raw endpoints.
DEFAULT_PRICE_VALIDATION = {
    "litellm_url": ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
                    "model_prices_and_context_window.json"),
    "openrouter_url": "https://openrouter.ai/api/v1/models",
    "litellm_display_url": ("https://github.com/BerriAI/litellm/blob/main/"
                            "model_prices_and_context_window.json"),
    "openrouter_display_url": "https://openrouter.ai/models",
    "tolerance_pct": 0.02,      # within this on BOTH fields -> match
    "review_band_pct": 0.10,    # beyond this on either field -> mismatch (real conflict)
    "model_keys": {
        "anthropic:claude-haiku-4-5": {
            "litellm": "claude-haiku-4-5",
            "openrouter": "anthropic/claude-haiku-4.5"},
        "openai:gpt-5.4-nano": {
            "litellm": "gpt-5.4-nano",
            "openrouter": "openai/gpt-5.4-nano"},
        "xai:grok-4.3": {
            "litellm": "xai/grok-4.3",
            "openrouter": "x-ai/grok-4.3"},
        "google:gemini-2.5-flash-lite": {
            "litellm": "gemini-2.5-flash-lite",
            "openrouter": "google/gemini-2.5-flash-lite"},
    },
}

# The dashboard Extract page spawns scripts/run-pipeline.sh and streams its stderr over
# SSE. These three knobs bound that run; none is a magic number inline.
# EXTRACT_RUN_TIMEOUT_SECONDS is a SAFETY CEILING, not a perf target: the single-flight
# lock lives for the child's whole lifetime, so a wedged run (a hung socket mid-fetch, a
# stuck DNS, a provider connection that never returns) would otherwise hold the lock until
# the dashboard is restarted. Sized well above the worst legitimate discover (searches +
# per-video classification with retries and backoff) so it ONLY ever fires on a true wedge.
# On expiry the run is SIGTERM'd, given EXTRACT_TERMINATE_GRACE_SECONDS to exit, then
# SIGKILL'd. EXTRACT_SSE_KEEPALIVE_SECONDS is how often the stream emits a comment ping
# while the run is quiet so proxies/clients don't drop an idle connection. Fresh-checkout
# fallbacks; settings.toml overrides them.
DEFAULT_EXTRACT_RUN_TIMEOUT_SECONDS = 1800
DEFAULT_EXTRACT_TERMINATE_GRACE_SECONDS = 10
DEFAULT_EXTRACT_SSE_KEEPALIVE_SECONDS = 15

# Raw-mode interpretation spend guard. The "raw" interpretation context dumps every
# distinct video row in the window to the model (potentially ~30x the fixed-size
# aggregated payload), so it is the token exposure. INTERP_RAW_COST_CAP_USD bounds the
# ESTIMATED cost of one raw run; a priced run estimated above it is refused before
# spending (aggregated mode is uncapped). The estimate turns the built prompt's char
# length into tokens via INTERP_CHARS_PER_TOKEN and assumes INTERP_EST_OUTPUT_TOKENS for
# the bounded JSON reply. chars/token is deliberately 3 (below the ~4 prose heuristic):
# the raw payload is dense JSON, which tokenizes at fewer chars per token, so 3 OVER-counts
# tokens rather than under-counting -- the safe direction for a spend guard.
DEFAULT_INTERP_RAW_COST_CAP_USD = 0.25
DEFAULT_INTERP_CHARS_PER_TOKEN = 3
DEFAULT_INTERP_EST_OUTPUT_TOKENS = 1500

# name -> default, for every externalized tunable. load_settings() merges the
# parsed TOML over these, so a missing key always resolves to its default.
SETTINGS_DEFAULTS: dict[str, object] = {
    "MIN_VIEWS": DEFAULT_MIN_VIEWS,
    "WINDOW_DAYS": DEFAULT_WINDOW_DAYS,
    "REFRESH_MAX_AGE_DAYS": DEFAULT_REFRESH_MAX_AGE_DAYS,
    "TOP_N": DEFAULT_TOP_N,
    "LIFECYCLE_COHORT_SIZE": DEFAULT_LIFECYCLE_COHORT_SIZE,
    "LIFECYCLE_GROWTH_COHORT_SIZE": DEFAULT_LIFECYCLE_GROWTH_COHORT_SIZE,
    "SHORT_MAX_SECONDS": DEFAULT_SHORT_MAX_SECONDS,
    "SEARCH_RELEVANCE_LANGUAGE": DEFAULT_SEARCH_RELEVANCE_LANGUAGE,
    "DAILY_QUOTA_LIMIT": DEFAULT_DAILY_QUOTA_LIMIT,
    "SAFETY_BUFFER": DEFAULT_SAFETY_BUFFER,
    "OLLAMA_BASE_URL": DEFAULT_OLLAMA_BASE_URL,
    "OLLAMA_TIMEOUT_SECONDS": DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    "CATEGORY_REGION": DEFAULT_CATEGORY_REGION,
    "SEARCH_QUERIES": DEFAULT_SEARCH_QUERIES,
    "PRICES": DEFAULT_PRICES,
    "SPEND_VIZ": DEFAULT_SPEND_VIZ,
    "PRICE_REFRESH": DEFAULT_PRICE_REFRESH,
    "EXTRACTION_MODEL": DEFAULT_EXTRACTION_MODEL,
    "CLASSIFICATION_ENABLED": DEFAULT_CLASSIFICATION_ENABLED,
    "CLASSIFICATION_MODEL": DEFAULT_CLASSIFICATION_MODEL,
    "CLASSIFICATION_FALLBACK_MODEL": DEFAULT_CLASSIFICATION_FALLBACK_MODEL,
    "CLASSIFICATION_RETRY": DEFAULT_CLASSIFICATION_RETRY,
    "KID_TITLE_KEYWORDS": DEFAULT_KID_TITLE_KEYWORDS,
    "PRICE_SOURCES": DEFAULT_PRICE_SOURCES,
    "PRICE_VALIDATION": DEFAULT_PRICE_VALIDATION,
    "EXTRACT_RUN_TIMEOUT_SECONDS": DEFAULT_EXTRACT_RUN_TIMEOUT_SECONDS,
    "EXTRACT_TERMINATE_GRACE_SECONDS": DEFAULT_EXTRACT_TERMINATE_GRACE_SECONDS,
    "EXTRACT_SSE_KEEPALIVE_SECONDS": DEFAULT_EXTRACT_SSE_KEEPALIVE_SECONDS,
    "INTERP_RAW_COST_CAP_USD": DEFAULT_INTERP_RAW_COST_CAP_USD,
    "INTERP_CHARS_PER_TOKEN": DEFAULT_INTERP_CHARS_PER_TOKEN,
    "INTERP_EST_OUTPUT_TOKENS": DEFAULT_INTERP_EST_OUTPUT_TOKENS,
}

# Resolve relative to THIS file, not CWD, so it works regardless of where the
# script is launched from.
_SETTINGS_PATH = Path(__file__).resolve().with_name("settings.toml")


class ConfigError(ValueError):
    """Raised when configuration is missing a key, has a wrong type, or holds an
    invalid SEARCH_QUERIES entry. Subclasses ValueError so callers can catch
    either."""


# Keys with NO in-code default: they MUST be present in settings.toml. Loaded by
# load_required_settings (strict, ConfigError on absence), NOT merged through
# SETTINGS_DEFAULTS. Type/range validation lives in validate_config.
REQUIRED_TOML_KEYS: dict[str, type] = {
    "DEFAULT_MAX_TOKENS": int,
    "DEFAULT_MAX_TOKENS_REASONING": int,
    "MAX_TOKENS_UPPER_BOUND": int,
}


def load_required_settings(path: Path | str = _SETTINGS_PATH) -> dict:
    """Load the keys that have NO safe-degrade fallback and so MUST be in the file.

    Unlike load_settings (a missing file/key degrades to its DEFAULT_*), this is
    deliberately strict: a missing file OR a missing key raises ConfigError. A
    silently-defaulted output-token cap is exactly the invisible cost surprise the
    per-model max_tokens work exists to kill, so these three keys fail loud. The
    blast radius is intentional — a bad/absent settings.toml hard-fails here where
    every other setting would degrade. Values are NOT range-checked here; run
    validate_config over the loaded module."""
    try:
        with open(path, "rb") as f:
            parsed = tomllib.load(f)
    except FileNotFoundError as e:
        raise ConfigError(
            f"settings.toml not found at {path}; the required keys "
            f"{sorted(REQUIRED_TOML_KEYS)} have no in-code fallback"
        ) from e
    out = {}
    for key in REQUIRED_TOML_KEYS:
        if key not in parsed:
            raise ConfigError(f"Missing required settings.toml key: {key}")
        out[key] = parsed[key]
    return out


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
REFRESH_MAX_AGE_DAYS = _settings["REFRESH_MAX_AGE_DAYS"]
TOP_N = _settings["TOP_N"]
LIFECYCLE_COHORT_SIZE = _settings["LIFECYCLE_COHORT_SIZE"]
LIFECYCLE_GROWTH_COHORT_SIZE = _settings["LIFECYCLE_GROWTH_COHORT_SIZE"]
SHORT_MAX_SECONDS = _settings["SHORT_MAX_SECONDS"]
SEARCH_RELEVANCE_LANGUAGE = _settings["SEARCH_RELEVANCE_LANGUAGE"]
DAILY_QUOTA_LIMIT = _settings["DAILY_QUOTA_LIMIT"]
SAFETY_BUFFER = _settings["SAFETY_BUFFER"]
OLLAMA_BASE_URL = _settings["OLLAMA_BASE_URL"]
OLLAMA_TIMEOUT_SECONDS = _settings["OLLAMA_TIMEOUT_SECONDS"]
CATEGORY_REGION = _settings["CATEGORY_REGION"]
SEARCH_QUERIES = _settings["SEARCH_QUERIES"]
PRICES = _settings["PRICES"]
SPEND_VIZ = _settings["SPEND_VIZ"]
PRICE_REFRESH = _settings["PRICE_REFRESH"]
EXTRACTION_MODEL = _settings["EXTRACTION_MODEL"]
CLASSIFICATION_ENABLED = _settings["CLASSIFICATION_ENABLED"]
CLASSIFICATION_MODEL = _settings["CLASSIFICATION_MODEL"]
CLASSIFICATION_FALLBACK_MODEL = _settings["CLASSIFICATION_FALLBACK_MODEL"]
CLASSIFICATION_RETRY = _settings["CLASSIFICATION_RETRY"]
KID_TITLE_KEYWORDS = _settings["KID_TITLE_KEYWORDS"]
PRICE_SOURCES = _settings["PRICE_SOURCES"]
PRICE_VALIDATION = _settings["PRICE_VALIDATION"]
EXTRACT_RUN_TIMEOUT_SECONDS = _settings["EXTRACT_RUN_TIMEOUT_SECONDS"]
EXTRACT_TERMINATE_GRACE_SECONDS = _settings["EXTRACT_TERMINATE_GRACE_SECONDS"]
EXTRACT_SSE_KEEPALIVE_SECONDS = _settings["EXTRACT_SSE_KEEPALIVE_SECONDS"]
INTERP_RAW_COST_CAP_USD = _settings["INTERP_RAW_COST_CAP_USD"]
INTERP_CHARS_PER_TOKEN = _settings["INTERP_CHARS_PER_TOKEN"]
INTERP_EST_OUTPUT_TOKENS = _settings["INTERP_EST_OUTPUT_TOKENS"]

# The per-model max_tokens defaults — strict, no fallback (see load_required_settings).
_required = load_required_settings()
DEFAULT_MAX_TOKENS = _required["DEFAULT_MAX_TOKENS"]
DEFAULT_MAX_TOKENS_REASONING = _required["DEFAULT_MAX_TOKENS_REASONING"]
MAX_TOKENS_UPPER_BOUND = _required["MAX_TOKENS_UPPER_BOUND"]

# Providers that run locally and free, so a NULL (uncapped) max_tokens is allowed:
# the cost ceiling that justifies a cap everywhere else does not apply, and runaway
# runtime is the timeout's job, not a token cap. A dedicated set — NOT llm's
# THINK_PROVIDERS (different concern: the think toggle vs the cap policy). "Must be
# capped" (the paid-model guarantee) is `provider not in LOCAL_PROVIDERS`.
LOCAL_PROVIDERS: frozenset[str] = frozenset({"ollama"})


def default_max_tokens(provider: str, is_reasoning) -> int | None:
    """Factory default output cap for a model, in ONE place (seed, v9 backfill, and
    insert_model all call this). None for a local/free provider (stored uncapped;
    the adapter omits its limit param); otherwise the reasoning-aware cloud default
    — a reasoning model gets the generous cap so thinking tokens do not starve the
    answer. `is_reasoning` accepts 0/1 or bool."""
    if provider in LOCAL_PROVIDERS:
        return None
    return DEFAULT_MAX_TOKENS_REASONING if is_reasoning else DEFAULT_MAX_TOKENS

PUBLISHED_AFTER = "2025-09-01T00:00:00Z"
PUBLISHED_BEFORE = None

# Persistent SQLite pipeline (Phase 1). PUBLISHED_AFTER is retained for the
# existing pipeline; get_published_after() is the forward-looking helper and is
# intentionally not wired into swipefile.py yet.
DB_PATH = "data/database/swipefile.db"
QUOTA_RESET_TZ = "America/Los_Angeles"

# Root folder for the dashboard Documentation page. Its immediate subfolders are
# tabs and the supported files inside them are documents (see dashboard/discovery.py).
# Resolved against the repo root like DB_PATH. This is the ONLY hardcoded value of
# that feature; tab names, document names, order, and counts are all discovered.
PUBLISH_ROOT = "docs/publish"

# All stored DB timestamps are Eastern, ISO-8601 with offset (never UTC, never
# naive) — see now_local_iso(). The YouTube API param in get_published_after()
# is the one exemption (it must be UTC/Z).
LOCAL_TZ = "America/New_York"

# --- Pipeline exit codes (run-pipeline.sh -> swipefile.py) -------------------
# Distinct, stable process exit codes so a scheduler can retry intelligently.
# This is the SINGLE source of truth: the codes AND the reason/status -> code
# mapping live here; nothing else defines a contract integer. Code 1 is reserved
# by run-pipeline.sh for "never reached the pipeline" (missing venv, failed
# backup) and is deliberately kept OUTSIDE the 2..8 range.
EXIT_OK = 0           # success, today's rows captured
EXIT_SUCCESS = EXIT_OK
EXIT_NETWORK = 2      # connectivity/transport/transient rate-limit, retries exhausted
EXIT_QUOTA = 3        # daily quota cap reached; NOT retryable same day
EXIT_AUTH = 4         # 401 / keyInvalid / missing-empty YOUTUBE_API_KEY
EXIT_OTHER = 5        # uncaught/unclassifiable, or CLI/usage/config error
EXIT_DB_LOCKED = 6    # sqlite3.OperationalError containing "database is locked"
EXIT_NO_ROWS = 7      # ran clean but committed zero rankings rows for run_date
EXIT_CLASSIFY_BUDGET = 8  # classify fallback-spend breaker tripped (primary throttled,
                          # per-run Haiku cap reached); distinct from a YouTube quota stop

# The single "is this a quota stop?" set. The pipeline usually stops on its own
# quota budget BEFORE the API's quotaExceeded fires, producing one of these run_log
# statuses; main() routes ALL of them through one check to EXIT_QUOTA so a future
# fourth quota status cannot fall back to the retryable default. These strings are
# produced by swipefile._pick_status (quota_guard_stop, quota_exceeded) and the
# pre-flight path (quota_preflight_stop); a test pins this set to those producers.
QUOTA_STOP_STATUSES = frozenset(
    {"quota_guard_stop", "quota_exceeded", "quota_preflight_stop"}
)

# Internal sentinel reason for a transport/connectivity failure (a DNS/connect
# error has no API "reason"). The choke point raises PipelineApiError with this
# reason; the mapping below recognizes it as EXIT_NETWORK. Defined once here so
# the raiser and the classifier share one string.
NETWORK_FAILURE_REASON = "network"


def exit_code_for_api_failure(reason: str, status: int | None) -> int:
    """Map a YouTube API terminal failure to a contract exit code, REASON-FIRST,
    status only as a fallback (never guess a specific code from status alone).

    Reason is the positively-identified `error_details[0]["reason"]`; status is the
    HTTP status. A bare 400 with no/other reason is a code defect, NOT an auth
    problem, so it maps to EXIT_OTHER rather than sending a human chasing keys."""
    if reason == "keyInvalid":
        return EXIT_AUTH
    if reason in ("quotaExceeded", "dailyLimitExceeded"):
        return EXIT_QUOTA
    if reason in ("rateLimitExceeded", "userRateLimitExceeded",
                  NETWORK_FAILURE_REASON):
        return EXIT_NETWORK
    # Status fallback only when the reason did not positively classify. 401 is
    # unambiguously auth; a bare 400 (badRequest with no/other reason) is a code
    # defect, so it falls through to EXIT_OTHER, not EXIT_AUTH.
    if status == 401:
        return EXIT_AUTH
    return EXIT_OTHER


VALID_BUCKETS = {"health", "habit"}

# View-count distribution buckets. Shared between the pipeline's tuning
# diagnostics (swipefile.py re-imports these) and the dashboard's distribution
# histogram. They live here, in the stdlib-only config module, so the dashboard
# can reuse the exact thresholds WITHOUT importing swipefile.py (which would drag
# in the whole API/network dependency tree). Do not redefine the thresholds
# elsewhere.
DISTRIBUTION_BUCKETS = (">=100k", "50-100k", "20-50k", "10-20k", "5-10k", "1-5k", "<1k")


def distribution_bucket(count: int) -> str:
    """Return the DISTRIBUTION_BUCKETS label a single raw view count falls into.
    Comparisons use raw integer thresholds; any k-notation is display-only and
    never reaches here, so 99,999 lands in '50-100k'. Single source of truth for
    the bucket boundaries (distribution_buckets tallies via this)."""
    if count >= 100_000:
        return ">=100k"
    if count >= 50_000:
        return "50-100k"
    if count >= 20_000:
        return "20-50k"
    if count >= 10_000:
        return "10-20k"
    if count >= 5_000:
        return "5-10k"
    if count >= 1_000:
        return "1-5k"
    return "<1k"


def distribution_buckets(counts: list[int]) -> dict[str, int]:
    """Tally raw view counts into DISTRIBUTION_BUCKETS, keyed in canonical order.
    Thresholds are defined once in distribution_bucket()."""
    out = {k: 0 for k in DISTRIBUTION_BUCKETS}
    for c in counts:
        out[distribution_bucket(c)] += 1
    return out

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
    "REFRESH_MAX_AGE_DAYS": int,
    "TOP_N": int,
    "LIFECYCLE_COHORT_SIZE": int,
    "LIFECYCLE_GROWTH_COHORT_SIZE": int,
    "DAILY_QUOTA_LIMIT": int,
    "SAFETY_BUFFER": int,
    "QUOTA_RESET_TZ": str,
    "LOCAL_TZ": str,
    "MIN_VIEWS": int,
    "SHORT_MAX_SECONDS": int,
    "SEARCH_RELEVANCE_LANGUAGE": str,
    "CATEGORY_REGION": str,
    "OLLAMA_BASE_URL": str,
    "OLLAMA_TIMEOUT_SECONDS": int,
    "SEARCH_QUERIES": list,
    "PRICES": dict,
    "CLASSIFICATION_ENABLED": bool,
    "CLASSIFICATION_RETRY": dict,
    "KID_TITLE_KEYWORDS": list,
    "DEFAULT_MAX_TOKENS": int,
    "DEFAULT_MAX_TOKENS_REASONING": int,
    "MAX_TOKENS_UPPER_BOUND": int,
    "EXTRACT_RUN_TIMEOUT_SECONDS": int,
    "EXTRACT_TERMINATE_GRACE_SECONDS": int,
    "EXTRACT_SSE_KEEPALIVE_SECONDS": int,
    "INTERP_RAW_COST_CAP_USD": float,
    "INTERP_CHARS_PER_TOKEN": int,
    "INTERP_EST_OUTPUT_TOKENS": int,
}

# Keys that must be strictly positive ints (type is checked via REQUIRED_KEYS).
POSITIVE_INT_KEYS: frozenset[str] = frozenset(
    {"MIN_VIEWS", "WINDOW_DAYS", "REFRESH_MAX_AGE_DAYS", "TOP_N",
     "LIFECYCLE_COHORT_SIZE", "LIFECYCLE_GROWTH_COHORT_SIZE", "SHORT_MAX_SECONDS",
     "DAILY_QUOTA_LIMIT", "SAFETY_BUFFER", "OLLAMA_TIMEOUT_SECONDS",
     "DEFAULT_MAX_TOKENS", "DEFAULT_MAX_TOKENS_REASONING", "MAX_TOKENS_UPPER_BOUND",
     "EXTRACT_RUN_TIMEOUT_SECONDS", "EXTRACT_TERMINATE_GRACE_SECONDS",
     "EXTRACT_SSE_KEEPALIVE_SECONDS",
     "INTERP_CHARS_PER_TOKEN", "INTERP_EST_OUTPUT_TOKENS"}
)


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


def validate_classifier_pair(
    primary: object,
    fallback: object,
    *,
    primary_label: str = "Config key CLASSIFICATION_MODEL",
    fallback_label: str = "Config key CLASSIFICATION_FALLBACK_MODEL",
) -> None:
    """Validate the classifier primary + fallback pair: each a non-empty
    'provider:model' string, and the two naming DIFFERENT providers. The fallback exists
    to survive a primary-provider outage, so a same-provider backup (or an identical
    model: same string -> same provider) is dead exactly when it is needed.

    Raises ConfigError. This is the ONE place the rule lives: validate_config calls it
    with the config-key labels (the cron's startup gate), and the dashboard model picker
    calls it with UI labels, so a UI write can never persist a pair the next run rejects.
    The label kwargs only change the message wording, not the rule."""
    for label, value in ((primary_label, primary), (fallback_label, fallback)):
        if not isinstance(value, str) or ":" not in value.strip():
            raise ConfigError(
                f"{label} must be a non-empty 'provider:model' string, got {value!r}"
            )
    if primary.split(":", 1)[0] == fallback.split(":", 1)[0]:
        raise ConfigError(
            f"{fallback_label} must use a DIFFERENT provider than {primary_label} "
            f"(both are {primary.split(':', 1)[0]!r}); the backup must survive a "
            "primary-provider outage"
        )


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

    # The raw-interpretation spend cap is a positive dollar amount (float; bool/int
    # already rejected by the REQUIRED_KEYS type check). A non-positive cap would refuse
    # every priced raw run.
    if getattr(cfg, "INTERP_RAW_COST_CAP_USD") <= 0:
        raise ConfigError("Config key INTERP_RAW_COST_CAP_USD must be > 0")

    # The factory defaults must not exceed the typo-guard ceiling. The reasoning
    # default deliberately sits BELOW the ceiling so a reasoning model keeps headroom
    # to be tuned up in /models without a config edit.
    bound = getattr(cfg, "MAX_TOKENS_UPPER_BOUND")
    if getattr(cfg, "DEFAULT_MAX_TOKENS") > bound:
        raise ConfigError(
            "Config key DEFAULT_MAX_TOKENS must be <= MAX_TOKENS_UPPER_BOUND"
        )
    if getattr(cfg, "DEFAULT_MAX_TOKENS_REASONING") > bound:
        raise ConfigError(
            "Config key DEFAULT_MAX_TOKENS_REASONING must be <= MAX_TOKENS_UPPER_BOUND"
        )

    # CATEGORY_REGION feeds the videoCategories.list regionCode; an empty string
    # would silently break the fetch, so reject it even though it types as str.
    if not getattr(cfg, "CATEGORY_REGION").strip():
        raise ConfigError("Config key CATEGORY_REGION must be a non-empty string")

    # OLLAMA_BASE_URL is the local model endpoint; an empty string would make the
    # adapter build a malformed request, so reject it even though it types as str.
    if not getattr(cfg, "OLLAMA_BASE_URL").strip():
        raise ConfigError("Config key OLLAMA_BASE_URL must be a non-empty string")

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

    # SPEND_VIZ: the dashboard spend-visual palette/thresholds/radii. Every color
    # and radius is a non-empty string; donut_slice_colors a non-empty list of
    # them; the two $/M cutoffs are non-negative numbers with good <= warn. A
    # hand-edit typo fails loud here, naming the offending key.
    viz = getattr(cfg, "SPEND_VIZ")
    if not isinstance(viz, dict):
        raise ConfigError(
            f"Config key SPEND_VIZ must be a dict, got {type(viz).__name__}"
        )
    string_keys = (
        "token_bar_color", "cost_bar_color", "donut_inner_radius",
        "donut_outer_radius", "good_bg", "good_fg", "mid_bg", "mid_fg",
        "warn_bg", "warn_fg",
    )
    for key in string_keys:
        value = viz.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"SPEND_VIZ['{key}'] must be a non-empty string, got {value!r}"
            )

    slices = viz.get("donut_slice_colors")
    if not isinstance(slices, list) or not slices:
        raise ConfigError(
            "SPEND_VIZ['donut_slice_colors'] must be a non-empty list"
        )
    for i, color in enumerate(slices):
        if not isinstance(color, str) or not color.strip():
            raise ConfigError(
                f"SPEND_VIZ['donut_slice_colors'][{i}] must be a non-empty string, "
                f"got {color!r}"
            )

    for key in ("efficiency_good", "efficiency_warn"):
        value = viz.get(key)
        # bool is an int subclass; reject it where a number is required.
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ConfigError(f"SPEND_VIZ['{key}'] must be a number, got {value!r}")
        if value < 0:
            raise ConfigError(f"SPEND_VIZ['{key}'] must be non-negative, got {value}")
    if viz["efficiency_good"] > viz["efficiency_warn"]:
        raise ConfigError(
            "SPEND_VIZ['efficiency_good'] must be <= SPEND_VIZ['efficiency_warn']"
        )

    # PRICE_REFRESH: the price-refresh agent's validation thresholds. Each of the
    # four keys must be a number (bool rejected). magnitude_lo a positive per-MTok
    # floor strictly below magnitude_hi; cross_tol and delta_threshold fractions in
    # (0, 1). A hand-edit typo fails loud here, naming the offending key.
    pr = getattr(cfg, "PRICE_REFRESH")
    if not isinstance(pr, dict):
        raise ConfigError(
            f"Config key PRICE_REFRESH must be a dict, got {type(pr).__name__}"
        )
    for key in ("magnitude_lo", "magnitude_hi", "cross_tol", "delta_threshold"):
        value = pr.get(key)
        # bool is an int subclass; reject it where a number is required.
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ConfigError(f"PRICE_REFRESH['{key}'] must be a number, got {value!r}")
    if pr["magnitude_lo"] <= 0:
        raise ConfigError("PRICE_REFRESH['magnitude_lo'] must be a positive number")
    if pr["magnitude_hi"] <= pr["magnitude_lo"]:
        raise ConfigError(
            "PRICE_REFRESH['magnitude_hi'] must be > PRICE_REFRESH['magnitude_lo']"
        )
    for key in ("cross_tol", "delta_threshold"):
        if not (0 < pr[key] < 1):
            raise ConfigError(f"PRICE_REFRESH['{key}'] must be a fraction in (0, 1)")
    ua = pr.get("user_agent")
    if not isinstance(ua, str) or not ua.strip():
        raise ConfigError(
            "PRICE_REFRESH['user_agent'] must be a non-empty string (the fetcher's UA)")

    # EXTRACTION_MODEL: the canonical provider:model that powers price extraction. A
    # non-empty string carrying a ':' (so split_model can dispatch a provider).
    extraction_model = getattr(cfg, "EXTRACTION_MODEL")
    if not isinstance(extraction_model, str) or ":" not in extraction_model.strip():
        raise ConfigError(
            "Config key EXTRACTION_MODEL must be a non-empty 'provider:model' string, "
            f"got {extraction_model!r}"
        )

    # CLASSIFICATION_MODEL / CLASSIFICATION_FALLBACK_MODEL: the primary + backup models
    # for the English/no-kids gate. The pair rule (both 'provider:model', DIFFERENT
    # providers) lives in validate_classifier_pair so the dashboard picker enforces the
    # SAME rule the cron does. API-key presence is a runtime concern (validate_config
    # cannot read os.environ for the operator's keys), warned at the phase, not here.
    validate_classifier_pair(
        getattr(cfg, "CLASSIFICATION_MODEL"),
        getattr(cfg, "CLASSIFICATION_FALLBACK_MODEL"),
    )

    # CLASSIFICATION_RETRY: the classify lane's retry/backoff/breaker policy. All four
    # keys required; max_retries and max_fallback_videos strictly positive ints (a bool
    # is an int subclass and rejected); the two backoffs strictly positive floats with
    # base <= max (an inverted band would clamp every wait to the smaller bound). Each
    # error names CLASSIFICATION_RETRY so a hand-edit typo is unambiguous.
    retry = getattr(cfg, "CLASSIFICATION_RETRY")
    if not isinstance(retry, dict):
        raise ConfigError(
            f"Config key CLASSIFICATION_RETRY must be a dict, got "
            f"{type(retry).__name__}"
        )
    for key in ("max_retries", "base_backoff_seconds", "max_backoff_seconds",
                "max_fallback_videos"):
        if key not in retry:
            raise ConfigError(
                f"Config key CLASSIFICATION_RETRY is missing required key {key!r}"
            )
    for key in ("max_retries", "max_fallback_videos"):
        value = retry[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(
                f"Config key CLASSIFICATION_RETRY[{key!r}] must be a positive int, "
                f"got {value!r}"
            )
    base = retry["base_backoff_seconds"]
    cap = retry["max_backoff_seconds"]
    for key, value in (("base_backoff_seconds", base), ("max_backoff_seconds", cap)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ConfigError(
                f"Config key CLASSIFICATION_RETRY[{key!r}] must be a positive number, "
                f"got {value!r}"
            )
    if base > cap:
        raise ConfigError(
            "Config key CLASSIFICATION_RETRY base_backoff_seconds must be <= "
            f"max_backoff_seconds (got {base} > {cap})"
        )

    # KID_TITLE_KEYWORDS: the deterministic kid-content title reject list. A non-empty
    # list of non-empty, already-lowercased strings (the matcher lowercases the title,
    # so an uppercase keyword could never match and is a config error, not silent dead
    # weight). A match is a PERMANENT drop, so a malformed list fails loud here.
    keywords = getattr(cfg, "KID_TITLE_KEYWORDS")
    if not isinstance(keywords, list) or not keywords:
        raise ConfigError(
            "Config key KID_TITLE_KEYWORDS must be a non-empty list, got "
            f"{type(keywords).__name__}"
        )
    for kw in keywords:
        if not isinstance(kw, str) or not kw.strip() or kw != kw.lower():
            raise ConfigError(
                "Config key KID_TITLE_KEYWORDS entries must be non-empty lowercase "
                f"strings, got {kw!r}"
            )

    # PRICE_SOURCES: ONE official pricing-page URL per NON-LOCAL seeded provider (local
    # providers have no pricing page). The scrape is cross-checked against a validator
    # feed, not a second scrape, so one URL is correct. A hand-edit typo (or a stale
    # list-of-2 from the old format) fails loud, naming the offending provider.
    sources = getattr(cfg, "PRICE_SOURCES")
    if not isinstance(sources, dict):
        raise ConfigError(
            f"Config key PRICE_SOURCES must be a dict, got {type(sources).__name__}"
        )
    required = {m["provider"] for m in SEED_MODELS} - LOCAL_PROVIDERS
    for provider in sorted(required):
        url = sources.get(provider)
        if not isinstance(url, str) or not url.strip():
            raise ConfigError(
                f"PRICE_SOURCES['{provider}'] must be one non-empty URL string"
            )

    # PRICE_VALIDATION: the third-party validator feeds + bands + model-key map. urls
    # non-empty strings; tolerance_pct/review_band_pct fractions in (0, 1) with tolerance
    # STRICTLY tighter than the review band (else the drift zone collapses); model_keys a
    # dict mapping each canonical 'provider:model' to BOTH a 'litellm' and 'openrouter'
    # non-empty id. Coverage of the live registry is a RUNTIME guard (needs a conn), not
    # checked here; this validates shape only. A hand-edit typo fails loud, naming the key.
    pv = getattr(cfg, "PRICE_VALIDATION")
    if not isinstance(pv, dict):
        raise ConfigError(
            f"Config key PRICE_VALIDATION must be a dict, got {type(pv).__name__}"
        )
    for key in ("litellm_url", "openrouter_url", "litellm_display_url",
                "openrouter_display_url"):
        val = pv.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ConfigError(
                f"PRICE_VALIDATION['{key}'] must be a non-empty URL string")
    for key in ("tolerance_pct", "review_band_pct"):
        value = pv.get(key)
        # bool is an int subclass; reject it where a fraction is required.
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ConfigError(
                f"PRICE_VALIDATION['{key}'] must be a number, got {value!r}")
        if not (0 < value < 1):
            raise ConfigError(
                f"PRICE_VALIDATION['{key}'] must be a fraction in (0, 1)")
    if pv["tolerance_pct"] >= pv["review_band_pct"]:
        raise ConfigError(
            "PRICE_VALIDATION['tolerance_pct'] must be < "
            "PRICE_VALIDATION['review_band_pct']")
    keys_map = pv.get("model_keys")
    if not isinstance(keys_map, dict):
        raise ConfigError(
            "PRICE_VALIDATION['model_keys'] must be a dict of "
            "'provider:model' -> {litellm, openrouter}")
    for model, entry in keys_map.items():
        if not isinstance(entry, dict):
            raise ConfigError(
                f"PRICE_VALIDATION['model_keys']['{model}'] must be a dict")
        for vkey in ("litellm", "openrouter"):
            vid = entry.get(vkey)
            if not isinstance(vid, str) or not vid.strip():
                raise ConfigError(
                    f"PRICE_VALIDATION['model_keys']['{model}']['{vkey}'] "
                    "must be a non-empty validator id string")


# Validate the loaded settings at import. An external settings.toml means
# external typos, so this is load-bearing: a malformed file fails loudly and
# immediately with a named ConfigError rather than corrupting a run downstream.
validate_config()
