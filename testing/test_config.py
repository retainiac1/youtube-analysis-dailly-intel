from types import SimpleNamespace

import pytest

import config


def make_good_config() -> SimpleNamespace:
    return SimpleNamespace(
        DB_PATH="swipefile.db",
        WINDOW_DAYS=3,
        TOP_N=20,
        DAILY_QUOTA_LIMIT=10000,
        SAFETY_BUFFER=500,
        QUOTA_RESET_TZ="America/Los_Angeles",
        LOCAL_TZ="America/New_York",
        MIN_VIEWS=100_000,
        SHORT_MAX_SECONDS=180,
        SEARCH_RELEVANCE_LANGUAGE="en",
        CATEGORY_REGION="US",
        OLLAMA_BASE_URL="http://127.0.0.1:11434",
        OLLAMA_TIMEOUT_SECONDS=600,
        DEFAULT_MAX_TOKENS=512,
        DEFAULT_MAX_TOKENS_REASONING=5000,
        MAX_TOKENS_UPPER_BOUND=8192,
        EXTRACT_RUN_TIMEOUT_SECONDS=1800,
        EXTRACT_TERMINATE_GRACE_SECONDS=10,
        EXTRACT_SSE_KEEPALIVE_SECONDS=15,
        SEARCH_QUERIES=[
            {"q": "build habits", "bucket": "habit"},
            {"q": "zone 2 cardio", "bucket": "health"},
        ],
        PRICES={
            "anthropic:claude-haiku-4-5": {"input": 1.00, "output": 5.00},
            "openai:gpt-5.4-nano": {"input": 0.20, "output": 1.25},
        },
        SPEND_VIZ={
            "token_bar_color": "#6FA8F5",
            "cost_bar_color": "#3FA66A",
            "donut_slice_colors": ["#14532D", "#16A34A"],
            "donut_inner_radius": "55%",
            "donut_outer_radius": "80%",
            "efficiency_good": 0.50,
            "efficiency_warn": 1.00,
            "good_bg": "#14532D",
            "good_fg": "#D1FAE5",
            "mid_bg": "#15803D",
            "mid_fg": "#ECFDF5",
            "warn_bg": "#4ADE80",
            "warn_fg": "#052E16",
        },
        PRICE_REFRESH={
            "magnitude_lo": 0.02,
            "magnitude_hi": 200.0,
            "cross_tol": 0.05,
            "delta_threshold": 0.10,
            "user_agent": "Mozilla/5.0 (Test) Chrome/124.0",
        },
        EXTRACTION_MODEL="anthropic:claude-haiku-4-5",
        CLASSIFICATION_ENABLED=True,
        CLASSIFICATION_MODEL="google:gemini-2.5-flash-lite",
        CLASSIFICATION_FALLBACK_MODEL="anthropic:claude-haiku-4-5",
        CLASSIFICATION_RETRY={
            "max_retries": 1,
            "base_backoff_seconds": 1.0,
            "max_backoff_seconds": 30.0,
            "max_fallback_videos": 150,
        },
        KID_TITLE_KEYWORDS=["baby", "kids", "toddler", "sahm"],
        PRICE_SOURCES={
            "anthropic": "https://a/official",
            "openai": "https://o/official",
            "xai": "https://x/official",
            "google": "https://g/official",
        },
        PRICE_VALIDATION={
            "litellm_url": "https://raw.example/litellm.json",
            "openrouter_url": "https://openrouter.example/api/v1/models",
            "litellm_display_url": "https://github.example/litellm",
            "openrouter_display_url": "https://openrouter.example/models",
            "tolerance_pct": 0.02,
            "review_band_pct": 0.10,
            "model_keys": {
                "anthropic:claude-haiku-4-5":
                    {"litellm": "claude-haiku-4-5",
                     "openrouter": "anthropic/claude-haiku-4.5"},
                "openai:gpt-5.4-nano":
                    {"litellm": "gpt-5.4-nano",
                     "openrouter": "openai/gpt-5.4-nano"},
                "xai:grok-4.3":
                    {"litellm": "xai/grok-4.3", "openrouter": "x-ai/grok-4.3"},
                "google:gemini-2.5-flash-lite":
                    {"litellm": "gemini-2.5-flash-lite",
                     "openrouter": "google/gemini-2.5-flash-lite"},
            },
        },
    )


def test_validate_config_passes_on_good_config():
    # Should not raise.
    config.validate_config(make_good_config())


def test_real_module_config_is_valid():
    # The actual config.py module must validate as shipped.
    config.validate_config()


def test_missing_key_raises_named_error():
    cfg = make_good_config()
    del cfg.DB_PATH
    with pytest.raises(config.ConfigError, match="DB_PATH"):
        config.validate_config(cfg)


def test_bad_type_raises_named_error():
    cfg = make_good_config()
    cfg.WINDOW_DAYS = "three"
    with pytest.raises(config.ConfigError, match="WINDOW_DAYS"):
        config.validate_config(cfg)


def test_bool_is_rejected_for_int_key():
    cfg = make_good_config()
    cfg.TOP_N = True  # bool is an int subclass; must still be rejected
    with pytest.raises(config.ConfigError, match="TOP_N"):
        config.validate_config(cfg)


def test_classification_model_bad_string_raises():
    cfg = make_good_config()
    cfg.CLASSIFICATION_MODEL = "no-colon-here"
    with pytest.raises(config.ConfigError, match="CLASSIFICATION_MODEL"):
        config.validate_config(cfg)


def test_classification_fallback_same_provider_raises():
    cfg = make_good_config()
    # Both google -> the backup would be dead exactly when the primary's provider is.
    cfg.CLASSIFICATION_FALLBACK_MODEL = "google:gemini-2.5-pro"
    with pytest.raises(config.ConfigError, match="CLASSIFICATION_FALLBACK_MODEL"):
        config.validate_config(cfg)


# --- CLASSIFICATION_RETRY (the classify retry/backoff/breaker policy) -------
# An otherwise-valid config with one bad CLASSIFICATION_RETRY key; the error must
# name CLASSIFICATION_RETRY so a hand-edit typo is unambiguous.

def test_classification_retry_missing_key_raises():
    cfg = make_good_config()
    del cfg.CLASSIFICATION_RETRY["max_fallback_videos"]
    with pytest.raises(config.ConfigError, match="CLASSIFICATION_RETRY"):
        config.validate_config(cfg)


def test_classification_retry_max_retries_must_be_positive_int():
    cfg = make_good_config()
    cfg.CLASSIFICATION_RETRY["max_retries"] = 0
    with pytest.raises(config.ConfigError, match="CLASSIFICATION_RETRY"):
        config.validate_config(cfg)


def test_classification_retry_max_retries_bool_rejected():
    cfg = make_good_config()
    cfg.CLASSIFICATION_RETRY["max_retries"] = True  # bool is an int subclass
    with pytest.raises(config.ConfigError, match="CLASSIFICATION_RETRY"):
        config.validate_config(cfg)


def test_classification_retry_base_gt_max_raises():
    cfg = make_good_config()
    cfg.CLASSIFICATION_RETRY["base_backoff_seconds"] = 60.0  # > max_backoff_seconds
    with pytest.raises(config.ConfigError, match="CLASSIFICATION_RETRY"):
        config.validate_config(cfg)


def test_classification_retry_max_fallback_videos_zero_raises():
    cfg = make_good_config()
    cfg.CLASSIFICATION_RETRY["max_fallback_videos"] = 0
    with pytest.raises(config.ConfigError, match="CLASSIFICATION_RETRY"):
        config.validate_config(cfg)


def test_classification_retry_max_fallback_videos_negative_raises():
    cfg = make_good_config()
    cfg.CLASSIFICATION_RETRY["max_fallback_videos"] = -5
    with pytest.raises(config.ConfigError, match="CLASSIFICATION_RETRY"):
        config.validate_config(cfg)


def test_classification_retry_backoff_must_be_positive():
    cfg = make_good_config()
    cfg.CLASSIFICATION_RETRY["base_backoff_seconds"] = 0.0
    with pytest.raises(config.ConfigError, match="CLASSIFICATION_RETRY"):
        config.validate_config(cfg)


# --- EXIT_CLASSIFY_BUDGET: distinct exit code for the fallback breaker -------

def test_kid_title_keywords_must_be_nonempty_list():
    cfg = make_good_config()
    cfg.KID_TITLE_KEYWORDS = []
    with pytest.raises(config.ConfigError, match="KID_TITLE_KEYWORDS"):
        config.validate_config(cfg)


def test_kid_title_keywords_members_must_be_nonempty_lowercase_strings():
    cfg = make_good_config()
    cfg.KID_TITLE_KEYWORDS = ["baby", "Kids"]  # uppercase member rejected
    with pytest.raises(config.ConfigError, match="KID_TITLE_KEYWORDS"):
        config.validate_config(cfg)


def test_kid_title_keywords_blank_member_rejected():
    cfg = make_good_config()
    cfg.KID_TITLE_KEYWORDS = ["baby", "  "]
    with pytest.raises(config.ConfigError, match="KID_TITLE_KEYWORDS"):
        config.validate_config(cfg)


def test_exit_classify_budget_is_distinct_code():
    # 8, outside the 2..7 contract range, and not colliding with any existing code.
    assert config.EXIT_CLASSIFY_BUDGET == 8
    existing = {config.EXIT_OK, config.EXIT_NETWORK, config.EXIT_QUOTA,
                config.EXIT_AUTH, config.EXIT_OTHER, config.EXIT_DB_LOCKED,
                config.EXIT_NO_ROWS}
    assert config.EXIT_CLASSIFY_BUDGET not in existing


def test_classification_enabled_must_be_bool():
    cfg = make_good_config()
    cfg.CLASSIFICATION_ENABLED = "yes"
    with pytest.raises(config.ConfigError, match="CLASSIFICATION_ENABLED"):
        config.validate_config(cfg)


def test_classification_cap_is_reasoning_aware_per_provider():
    # The fallback's cap must absorb hidden reasoning tokens; the primary's need not.
    # Covers both axes (provider + is_reasoning) the cap factory keys on.
    seeds = {m["model"]: m for m in config.SEED_MODELS}
    nano = seeds["openai:gpt-5.4-nano"]
    gemini = seeds["google:gemini-2.5-flash-lite"]
    assert nano["is_reasoning"] == 1 and nano["supports_temperature"] == 0
    assert gemini["is_reasoning"] == 0
    assert config.default_max_tokens("openai", nano["is_reasoning"]) == \
        config.DEFAULT_MAX_TOKENS_REASONING
    assert config.default_max_tokens("google", gemini["is_reasoning"]) == \
        config.DEFAULT_MAX_TOKENS
    # A local provider stays uncapped regardless of the reasoning axis.
    assert config.default_max_tokens("ollama", True) is None


def test_invalid_bucket_raises_named_error():
    cfg = make_good_config()
    cfg.SEARCH_QUERIES = [{"q": "build habits", "bucket": "trending"}]
    with pytest.raises(config.ConfigError, match="SEARCH_QUERIES"):
        config.validate_config(cfg)


def test_empty_query_string_raises():
    cfg = make_good_config()
    cfg.SEARCH_QUERIES = [{"q": "  ", "bucket": "habit"}]
    with pytest.raises(config.ConfigError, match="SEARCH_QUERIES"):
        config.validate_config(cfg)


def test_bad_min_views_type_raises_named_error():
    cfg = make_good_config()
    cfg.MIN_VIEWS = "lots"
    with pytest.raises(config.ConfigError, match="MIN_VIEWS"):
        config.validate_config(cfg)


def test_non_positive_min_views_raises_named_error():
    cfg = make_good_config()
    cfg.MIN_VIEWS = 0
    with pytest.raises(config.ConfigError, match="MIN_VIEWS"):
        config.validate_config(cfg)


def test_toml_boolean_for_int_key_is_rejected():
    # A hand-edited `TOP_N = true` in settings.toml parses to a Python bool, and
    # isinstance(True, int) is True — the bool-rejection must still fire.
    cfg = make_good_config()
    cfg.TOP_N = True
    with pytest.raises(config.ConfigError, match="TOP_N"):
        config.validate_config(cfg)


def test_safety_buffer_not_below_limit_raises_named_error():
    cfg = make_good_config()
    cfg.SAFETY_BUFFER = cfg.DAILY_QUOTA_LIMIT
    with pytest.raises(config.ConfigError, match="SAFETY_BUFFER"):
        config.validate_config(cfg)


def test_empty_search_queries_raises_named_error():
    cfg = make_good_config()
    cfg.SEARCH_QUERIES = []
    with pytest.raises(config.ConfigError, match="SEARCH_QUERIES"):
        config.validate_config(cfg)


def test_missing_category_region_raises_named_error():
    cfg = make_good_config()
    del cfg.CATEGORY_REGION
    with pytest.raises(config.ConfigError, match="CATEGORY_REGION"):
        config.validate_config(cfg)


def test_empty_category_region_raises_named_error():
    cfg = make_good_config()
    cfg.CATEGORY_REGION = "  "  # whitespace-only would silently break the fetch
    with pytest.raises(config.ConfigError, match="CATEGORY_REGION"):
        config.validate_config(cfg)


def test_empty_ollama_base_url_raises_named_error():
    cfg = make_good_config()
    cfg.OLLAMA_BASE_URL = "  "  # whitespace-only would build a malformed request
    with pytest.raises(config.ConfigError, match="OLLAMA_BASE_URL"):
        config.validate_config(cfg)


def test_non_positive_ollama_timeout_raises_named_error():
    cfg = make_good_config()
    cfg.OLLAMA_TIMEOUT_SECONDS = 0  # a zero/negative timeout would hang or error
    with pytest.raises(config.ConfigError, match="OLLAMA_TIMEOUT_SECONDS"):
        config.validate_config(cfg)


@pytest.mark.parametrize(
    "key",
    [
        "EXTRACT_RUN_TIMEOUT_SECONDS",
        "EXTRACT_TERMINATE_GRACE_SECONDS",
        "EXTRACT_SSE_KEEPALIVE_SECONDS",
    ],
)
def test_non_positive_extract_knob_raises_named_error(key):
    # The Extract-page watchdog/keepalive seconds bound a spawned run; a zero or
    # negative value would make the watchdog fire instantly or busy-loop the stream.
    cfg = make_good_config()
    setattr(cfg, key, 0)
    with pytest.raises(config.ConfigError, match=key):
        config.validate_config(cfg)


def test_prices_well_formed_passes():
    # Positive control: a good [prices] map validates cleanly, so the negatives
    # below are not passing for an unrelated reason.
    config.validate_config(make_good_config())


def test_prices_non_dict_entry_raises_named_error():
    # Otherwise-valid config; only PRICES is malformed, so the failure must come
    # from the price loop (which runs last), not an earlier required-key check.
    cfg = make_good_config()
    cfg.PRICES = {"openai:gpt-5.4-nano": 1.25}  # value should be a dict
    with pytest.raises(config.ConfigError, match=r"openai:gpt-5\.4-nano"):
        config.validate_config(cfg)


def test_prices_missing_output_raises_named_error():
    cfg = make_good_config()
    cfg.PRICES = {"openai:gpt-5.4-nano": {"input": 0.20}}
    with pytest.raises(config.ConfigError, match=r"openai:gpt-5\.4-nano"):
        config.validate_config(cfg)


def test_prices_negative_price_raises_named_error():
    cfg = make_good_config()
    cfg.PRICES = {"openai:gpt-5.4-nano": {"input": -0.20, "output": 1.25}}
    with pytest.raises(config.ConfigError, match=r"openai:gpt-5\.4-nano"):
        config.validate_config(cfg)


def test_prices_string_price_raises_named_error():
    cfg = make_good_config()
    cfg.PRICES = {"openai:gpt-5.4-nano": {"input": "cheap", "output": 1.25}}
    with pytest.raises(config.ConfigError, match=r"openai:gpt-5\.4-nano"):
        config.validate_config(cfg)


def test_settings_toml_has_quoted_price_keys():
    # Guards against an accidental unquoted [prices] key regressing settings.toml:
    # the four seeded canonical model keys must round-trip through load_settings.
    prices = config.load_settings()["PRICES"]
    for key in (
        "anthropic:claude-haiku-4-5",
        "openai:gpt-5.4-nano",
        "xai:grok-4.3",
        "google:gemini-2.5-flash-lite",
    ):
        assert key in prices, key


def test_spend_viz_well_formed_passes():
    # Positive control: a good SPEND_VIZ validates cleanly, so the negatives below
    # are not passing for an unrelated reason.
    config.validate_config(make_good_config())


def test_spend_viz_empty_color_raises_named_error():
    # Otherwise-valid config; only one SPEND_VIZ color is blank.
    cfg = make_good_config()
    cfg.SPEND_VIZ = {**cfg.SPEND_VIZ, "token_bar_color": ""}
    with pytest.raises(config.ConfigError, match="token_bar_color"):
        config.validate_config(cfg)


def test_spend_viz_empty_slice_list_raises_named_error():
    cfg = make_good_config()
    cfg.SPEND_VIZ = {**cfg.SPEND_VIZ, "donut_slice_colors": []}
    with pytest.raises(config.ConfigError, match="donut_slice_colors"):
        config.validate_config(cfg)


def test_spend_viz_non_string_slice_raises_named_error():
    cfg = make_good_config()
    cfg.SPEND_VIZ = {**cfg.SPEND_VIZ, "donut_slice_colors": ["#fff", 5]}
    with pytest.raises(config.ConfigError, match="donut_slice_colors"):
        config.validate_config(cfg)


def test_spend_viz_bool_threshold_is_rejected():
    # bool is an int subclass; a hand-edited `efficiency_good = true` must fail.
    cfg = make_good_config()
    cfg.SPEND_VIZ = {**cfg.SPEND_VIZ, "efficiency_good": True}
    with pytest.raises(config.ConfigError, match="efficiency_good"):
        config.validate_config(cfg)


def test_spend_viz_negative_threshold_raises_named_error():
    cfg = make_good_config()
    cfg.SPEND_VIZ = {**cfg.SPEND_VIZ, "efficiency_warn": -1.0}
    with pytest.raises(config.ConfigError, match="efficiency_warn"):
        config.validate_config(cfg)


def test_spend_viz_good_above_warn_raises_named_error():
    cfg = make_good_config()
    cfg.SPEND_VIZ = {**cfg.SPEND_VIZ, "efficiency_good": 2.0, "efficiency_warn": 1.0}
    with pytest.raises(config.ConfigError, match="efficiency_good"):
        config.validate_config(cfg)


def test_settings_toml_spend_viz_round_trips():
    # Guards against an accidental lowercase/dotted [SPEND_VIZ] section name, which
    # would silently fall back to DEFAULT_SPEND_VIZ instead of applying the file.
    viz = config.load_settings()["SPEND_VIZ"]
    assert viz is not config.DEFAULT_SPEND_VIZ
    assert viz["token_bar_color"] == "#2D7CFF"
    assert viz["efficiency_good"] == 0.50


def test_config_error_is_value_error():
    assert issubclass(config.ConfigError, ValueError)


# --- max_tokens per-model caps (v9) -----------------------------------------

def test_real_module_max_tokens_values():
    # The three caps live ONLY in settings.toml (strict, no in-code fallback).
    assert config.DEFAULT_MAX_TOKENS == 512
    assert config.DEFAULT_MAX_TOKENS_REASONING == 5000
    assert config.MAX_TOKENS_UPPER_BOUND == 8192


def test_local_providers_contains_ollama():
    # "Must be capped" = provider not in LOCAL_PROVIDERS; ollama is the local/free
    # provider that may stay uncapped (NULL).
    assert "ollama" in config.LOCAL_PROVIDERS


def test_default_max_tokens_local_is_uncapped():
    # A local/free provider gets None: the row stores no cap, the adapter omits the
    # limit param. is_reasoning is irrelevant for a local model.
    assert config.default_max_tokens("ollama", 1) is None
    assert config.default_max_tokens("ollama", 0) is None


def test_default_max_tokens_cloud_non_reasoning():
    assert config.default_max_tokens("anthropic", 0) == config.DEFAULT_MAX_TOKENS


def test_default_max_tokens_cloud_reasoning():
    assert config.default_max_tokens("openai", 1) == config.DEFAULT_MAX_TOKENS_REASONING


def test_non_positive_default_max_tokens_raises_named_error():
    cfg = make_good_config()
    cfg.DEFAULT_MAX_TOKENS = 0
    with pytest.raises(config.ConfigError, match="DEFAULT_MAX_TOKENS"):
        config.validate_config(cfg)


def test_default_max_tokens_over_bound_raises_named_error():
    # The factory default must not exceed the typo-guard ceiling.
    cfg = make_good_config()
    cfg.DEFAULT_MAX_TOKENS = cfg.MAX_TOKENS_UPPER_BOUND + 1
    with pytest.raises(config.ConfigError, match="DEFAULT_MAX_TOKENS"):
        config.validate_config(cfg)


def test_reasoning_default_over_bound_raises_named_error():
    # The reasoning default sits below the ceiling so a reasoning model has headroom
    # to be tuned up; an inverted config (default above ceiling) must fail.
    cfg = make_good_config()
    cfg.DEFAULT_MAX_TOKENS_REASONING = cfg.MAX_TOKENS_UPPER_BOUND + 1
    with pytest.raises(config.ConfigError, match="DEFAULT_MAX_TOKENS_REASONING"):
        config.validate_config(cfg)


def test_bool_rejected_for_max_tokens_key():
    # A hand-edited `DEFAULT_MAX_TOKENS = true` in TOML parses to a bool.
    cfg = make_good_config()
    cfg.DEFAULT_MAX_TOKENS = True
    with pytest.raises(config.ConfigError, match="DEFAULT_MAX_TOKENS"):
        config.validate_config(cfg)


# --- price-refresh thresholds ([PRICE_REFRESH]) -----------------------------

def test_price_refresh_well_formed_passes():
    # Positive control: a good PRICE_REFRESH validates cleanly, so the negatives
    # below are not passing for an unrelated reason.
    config.validate_config(make_good_config())


def test_real_module_price_refresh_values():
    # The thresholds live in settings.toml and load onto the module.
    pr = config.PRICE_REFRESH
    assert pr["magnitude_lo"] == 0.02
    assert pr["magnitude_hi"] == 200.0
    assert pr["cross_tol"] == 0.05
    assert pr["delta_threshold"] == 0.10


def test_settings_toml_price_refresh_round_trips():
    # Guards against a lowercase/dotted [PRICE_REFRESH] section name, which would
    # silently fall back to DEFAULT_PRICE_REFRESH instead of applying the file.
    pr = config.load_settings()["PRICE_REFRESH"]
    assert pr is not config.DEFAULT_PRICE_REFRESH
    assert pr["delta_threshold"] == 0.10


def test_price_refresh_missing_key_raises_named_error():
    cfg = make_good_config()
    cfg.PRICE_REFRESH = {k: v for k, v in cfg.PRICE_REFRESH.items()
                         if k != "delta_threshold"}
    with pytest.raises(config.ConfigError, match="delta_threshold"):
        config.validate_config(cfg)


def test_price_refresh_bool_threshold_rejected():
    # bool is an int subclass; a hand-edited `delta_threshold = true` must fail.
    cfg = make_good_config()
    cfg.PRICE_REFRESH = {**cfg.PRICE_REFRESH, "delta_threshold": True}
    with pytest.raises(config.ConfigError, match="delta_threshold"):
        config.validate_config(cfg)


def test_price_refresh_non_positive_magnitude_lo_raises():
    cfg = make_good_config()
    cfg.PRICE_REFRESH = {**cfg.PRICE_REFRESH, "magnitude_lo": 0}
    with pytest.raises(config.ConfigError, match="magnitude_lo"):
        config.validate_config(cfg)


def test_price_refresh_hi_not_above_lo_raises():
    cfg = make_good_config()
    cfg.PRICE_REFRESH = {**cfg.PRICE_REFRESH, "magnitude_lo": 5.0,
                         "magnitude_hi": 5.0}
    with pytest.raises(config.ConfigError, match="magnitude_hi"):
        config.validate_config(cfg)


def test_price_refresh_delta_threshold_out_of_range_raises():
    # The delta boundary is a fraction in (0, 1); 1.5 (150%) is a typo.
    cfg = make_good_config()
    cfg.PRICE_REFRESH = {**cfg.PRICE_REFRESH, "delta_threshold": 1.5}
    with pytest.raises(config.ConfigError, match="delta_threshold"):
        config.validate_config(cfg)


def test_price_refresh_cross_tol_out_of_range_raises():
    cfg = make_good_config()
    cfg.PRICE_REFRESH = {**cfg.PRICE_REFRESH, "cross_tol": 0}
    with pytest.raises(config.ConfigError, match="cross_tol"):
        config.validate_config(cfg)


def test_price_refresh_user_agent_required_non_empty():
    cfg = make_good_config()
    cfg.PRICE_REFRESH = {**cfg.PRICE_REFRESH, "user_agent": "  "}
    with pytest.raises(config.ConfigError, match="user_agent"):
        config.validate_config(cfg)


def test_real_module_price_refresh_has_user_agent():
    # The fetcher's UA lives in settings.toml, not a code literal.
    ua = config.PRICE_REFRESH["user_agent"]
    assert isinstance(ua, str) and ua.strip()


# --- extraction model + price sources (Phase 1) -----------------------------

def test_extraction_config_well_formed_passes():
    config.validate_config(make_good_config())


def test_real_module_extraction_model_value():
    assert ":" in config.EXTRACTION_MODEL
    assert config.EXTRACTION_MODEL.strip() == config.EXTRACTION_MODEL


def test_real_module_price_sources_cover_seeded_providers():
    seeded = {m["provider"] for m in config.SEED_MODELS} - config.LOCAL_PROVIDERS
    for provider in seeded:
        url = config.PRICE_SOURCES[provider]
        assert isinstance(url, str) and url.strip(), provider


def test_settings_toml_price_sources_round_trips():
    # Guards against a lowercase/dotted [PRICE_SOURCES] section name silently falling
    # back to DEFAULT_PRICE_SOURCES.
    src = config.load_settings()["PRICE_SOURCES"]
    assert src is not config.DEFAULT_PRICE_SOURCES
    assert "anthropic" in src


def test_extraction_model_empty_raises():
    cfg = make_good_config()
    cfg.EXTRACTION_MODEL = "  "
    with pytest.raises(config.ConfigError, match="EXTRACTION_MODEL"):
        config.validate_config(cfg)


def test_extraction_model_without_colon_raises():
    cfg = make_good_config()
    cfg.EXTRACTION_MODEL = "claude-haiku"  # not a provider:model
    with pytest.raises(config.ConfigError, match="EXTRACTION_MODEL"):
        config.validate_config(cfg)


def test_price_sources_missing_seeded_provider_raises():
    cfg = make_good_config()
    cfg.PRICE_SOURCES = {k: v for k, v in cfg.PRICE_SOURCES.items()
                         if k != "anthropic"}
    with pytest.raises(config.ConfigError, match="anthropic"):
        config.validate_config(cfg)


def test_price_sources_non_string_raises():
    # One URL string per provider now (not a list). A list is a stale-format typo.
    cfg = make_good_config()
    cfg.PRICE_SOURCES = {**cfg.PRICE_SOURCES, "openai": ["https://only-one"]}
    with pytest.raises(config.ConfigError, match="openai"):
        config.validate_config(cfg)


def test_price_sources_blank_url_raises():
    cfg = make_good_config()
    cfg.PRICE_SOURCES = {**cfg.PRICE_SOURCES, "xai": "  "}
    with pytest.raises(config.ConfigError, match="xai"):
        config.validate_config(cfg)


def test_price_sources_excludes_local_providers():
    # ollama is local — it must NOT be required in PRICE_SOURCES, so a config without
    # an ollama entry still validates.
    cfg = make_good_config()
    assert "ollama" not in cfg.PRICE_SOURCES
    config.validate_config(cfg)  # does not raise


# --- price validation feed config ([PRICE_VALIDATION], Phase B) --------------

def test_price_validation_well_formed_passes():
    config.validate_config(make_good_config())


def test_real_module_price_validation_round_trips():
    # Guards against a lowercase/dotted [PRICE_VALIDATION] section name silently
    # falling back to DEFAULT_PRICE_VALIDATION.
    pv = config.load_settings()["PRICE_VALIDATION"]
    assert pv is not config.DEFAULT_PRICE_VALIDATION
    assert pv["litellm_url"].strip() and pv["openrouter_url"].strip()


def test_real_module_price_validation_model_keys_cover_seeded():
    # Every non-local seeded model needs a model_keys entry carrying BOTH validator
    # ids, or the runtime completeness guard would flag it as "no key mapping".
    pv = config.PRICE_VALIDATION
    seeded = {m["model"] for m in config.SEED_MODELS
              if m["provider"] not in config.LOCAL_PROVIDERS}
    for model in seeded:
        entry = pv["model_keys"][model]
        assert entry["litellm"].strip() and entry["openrouter"].strip(), model


def test_price_validation_blank_url_raises():
    cfg = make_good_config()
    cfg.PRICE_VALIDATION = {**cfg.PRICE_VALIDATION, "litellm_url": "  "}
    with pytest.raises(config.ConfigError, match="litellm_url"):
        config.validate_config(cfg)


def test_price_validation_bool_band_rejected():
    # bool is an int subclass; reject it where a fraction is required.
    cfg = make_good_config()
    cfg.PRICE_VALIDATION = {**cfg.PRICE_VALIDATION, "tolerance_pct": True}
    with pytest.raises(config.ConfigError, match="tolerance_pct"):
        config.validate_config(cfg)


def test_price_validation_band_out_of_range_raises():
    cfg = make_good_config()
    cfg.PRICE_VALIDATION = {**cfg.PRICE_VALIDATION, "review_band_pct": 1.5}
    with pytest.raises(config.ConfigError, match="review_band_pct"):
        config.validate_config(cfg)


def test_price_validation_tolerance_not_below_review_band_raises():
    # tolerance (match) must be strictly tighter than the review band, else the
    # drift zone collapses.
    cfg = make_good_config()
    cfg.PRICE_VALIDATION = {**cfg.PRICE_VALIDATION,
                            "tolerance_pct": 0.10, "review_band_pct": 0.10}
    with pytest.raises(config.ConfigError, match="tolerance_pct"):
        config.validate_config(cfg)


def test_price_validation_model_keys_missing_validator_id_raises():
    cfg = make_good_config()
    broken = {**cfg.PRICE_VALIDATION["model_keys"],
              "xai:grok-4.3": {"litellm": "xai/grok-4.3"}}  # no openrouter
    cfg.PRICE_VALIDATION = {**cfg.PRICE_VALIDATION, "model_keys": broken}
    with pytest.raises(config.ConfigError, match="openrouter"):
        config.validate_config(cfg)


# --- exit_code_for_api_failure (scheduler retry contract) -------------------

@pytest.mark.parametrize("reason,status,expected", [
    # Reason-first: positively-identified API reasons win over status.
    ("keyInvalid", 400, config.EXIT_AUTH),
    ("keyInvalid", None, config.EXIT_AUTH),
    ("quotaExceeded", 403, config.EXIT_QUOTA),
    ("dailyLimitExceeded", 403, config.EXIT_QUOTA),
    ("rateLimitExceeded", 403, config.EXIT_NETWORK),
    ("userRateLimitExceeded", 403, config.EXIT_NETWORK),
    (config.NETWORK_FAILURE_REASON, None, config.EXIT_NETWORK),
    # Status fallback only when reason did not classify.
    ("", 401, config.EXIT_AUTH),
    # A bare 400 (badRequest / no reason) is a code defect, NOT auth.
    ("", 400, config.EXIT_OTHER),
    ("badRequest", 400, config.EXIT_OTHER),
    # Unknown reason / server error -> unclassifiable.
    ("", 500, config.EXIT_OTHER),
    ("somethingNew", 403, config.EXIT_OTHER),
])
def test_exit_code_for_api_failure(reason, status, expected):
    assert config.exit_code_for_api_failure(reason, status) == expected
