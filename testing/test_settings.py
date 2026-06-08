"""Tests for the settings.toml loader in config.load_settings().

These exercise the externalization purely through tmp files — no network, no DB,
and no dependence on the real settings.toml beyond the regression assertions that
the shipped module still loads the expected values.
"""

import config

VALID_TOML = """\
MIN_VIEWS = 12345
WINDOW_DAYS = 5
TOP_N = 7
SHORT_MAX_SECONDS = 90
SEARCH_RELEVANCE_LANGUAGE = "es"
DAILY_QUOTA_LIMIT = 9000
SAFETY_BUFFER = 250

[[SEARCH_QUERIES]]
q = "build habits"
bucket = "habit"

[[SEARCH_QUERIES]]
q = "zone 2 cardio"
bucket = "health"
"""


def _write(tmp_path, text):
    path = tmp_path / "settings.toml"
    path.write_text(text)
    return path


def test_valid_file_yields_expected_typed_values(tmp_path):
    loaded = config.load_settings(_write(tmp_path, VALID_TOML))
    assert loaded["MIN_VIEWS"] == 12345
    assert isinstance(loaded["MIN_VIEWS"], int)
    assert loaded["WINDOW_DAYS"] == 5
    assert loaded["TOP_N"] == 7
    assert loaded["SHORT_MAX_SECONDS"] == 90
    assert loaded["SEARCH_RELEVANCE_LANGUAGE"] == "es"
    assert loaded["DAILY_QUOTA_LIMIT"] == 9000
    assert loaded["SAFETY_BUFFER"] == 250
    assert loaded["SEARCH_QUERIES"] == [
        {"q": "build habits", "bucket": "habit"},
        {"q": "zone 2 cardio", "bucket": "health"},
    ]


def test_missing_key_falls_back_to_default(tmp_path):
    # WINDOW_DAYS omitted; everything else present.
    partial = "\n".join(
        line for line in VALID_TOML.splitlines() if not line.startswith("WINDOW_DAYS")
    )
    loaded = config.load_settings(_write(tmp_path, partial))
    assert loaded["WINDOW_DAYS"] == config.DEFAULT_WINDOW_DAYS
    # A present key is still its file value, proving only the gap was filled.
    assert loaded["MIN_VIEWS"] == 12345


def test_missing_file_falls_back_to_all_defaults_with_warning(tmp_path, capsys):
    bad_path = tmp_path / "does_not_exist.toml"
    loaded = config.load_settings(bad_path)

    # Value is the default itself, not merely "did not crash".
    assert loaded["MIN_VIEWS"] == config.DEFAULT_MIN_VIEWS
    assert loaded["SEARCH_QUERIES"] == config.DEFAULT_SEARCH_QUERIES
    assert loaded == config.SETTINGS_DEFAULTS

    warning = capsys.readouterr().err
    assert "not found" in warning


def test_real_module_unchanged_after_load():
    # The shipped settings.toml is a behavior-preserving extraction: a move, not
    # a retune. If any value drifted, that is a bug.
    assert config.MIN_VIEWS == 10000
    assert config.WINDOW_DAYS == 3
    assert config.TOP_N == 20
    assert config.SHORT_MAX_SECONDS == 180
    assert config.SEARCH_RELEVANCE_LANGUAGE == "en"
    assert config.DAILY_QUOTA_LIMIT == 10000
    assert config.SAFETY_BUFFER == 500
    assert len(config.SEARCH_QUERIES) == 12
