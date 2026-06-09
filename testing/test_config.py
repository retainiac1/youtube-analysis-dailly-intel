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
        SEARCH_QUERIES=[
            {"q": "build habits", "bucket": "habit"},
            {"q": "zone 2 cardio", "bucket": "health"},
        ],
        PRICES={
            "anthropic:claude-haiku-4-5": {"input": 1.00, "output": 5.00},
            "openai:gpt-5.4-nano": {"input": 0.20, "output": 1.25},
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
        "xai:grok-4-fast",
        "google:gemini-2.5-flash-lite",
    ):
        assert key in prices, key


def test_config_error_is_value_error():
    assert issubclass(config.ConfigError, ValueError)
