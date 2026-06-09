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


def test_config_error_is_value_error():
    assert issubclass(config.ConfigError, ValueError)
