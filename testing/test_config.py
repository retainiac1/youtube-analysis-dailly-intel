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


def test_config_error_is_value_error():
    assert issubclass(config.ConfigError, ValueError)
