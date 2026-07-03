import os
import sys

# Put the repo root on sys.path so `import config` / `import db` and
# `from dashboard.app import app` resolve when pytest runs from anywhere.
REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import pytest  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402


@pytest.fixture
def seeded_db_path(tmp_path):
    """A temp DB with one row per table the Phase 0 endpoints read. The
    quota_ledger row is keyed by today's Pacific date so /api/quota sees it
    regardless of the calendar day the test runs."""
    db_path = str(tmp_path / "dash.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO videos (video_id, title, channel_title, link, "
            "thumbnail_url, view_count, views_to_subs_ratio) VALUES "
            "('vid1', 'First video', 'Chan A', 'http://yt/vid1', "
            "'http://thumb/1', 1234, 9.5)"
        )
        conn.execute(
            "INSERT INTO rankings (run_date, bucket, rank, video_id, "
            "metric_value, captured_at) VALUES "
            "('2026-06-08', 'health', 1, 'vid1', 9.5, "
            "'2026-06-08T10:00:00-04:00')"
        )
        conn.execute(
            "INSERT INTO interpretations (window_key, scope, start_date, end_date, "
            "run_date, text, model, generated_at) VALUES "
            "('2026-06-08:2026-06-08', 'health', '2026-06-08', '2026-06-08', "
            "'2026-06-08', 'Looks strong.', 'model-x', '2026-06-08T10:05:00-04:00')"
        )
        conn.execute(
            "INSERT INTO quota_ledger (pacific_date, units_used, updated_at) "
            "VALUES (?, 4200, '2026-06-08T10:00:00-04:00')",
            (config.pacific_date(),),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture
def client(seeded_db_path):
    from fastapi.testclient import TestClient

    from dashboard.app import app, get_db_path

    app.dependency_overrides[get_db_path] = lambda: seeded_db_path
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# --- Phase 1 (library browse + filters) fixtures ----------------------------

# Channels: id -> (subscriber_count, country). Title is denormalized onto videos.
_LIB_CHANNELS = [
    ("chA", 1000, "US"),
    ("chB", 5000, "GB"),
    ("chC", 2000, "US"),
]

# One row per video. Columns chosen to exercise every Phase 1 filter:
# (video_id, title, channel_id, channel_title, published_at, duration_seconds,
#  is_short, view_count, views_to_subs_ratio, first_seen_at, matched_queries,
#  buckets, starred, user_notes)
# Note vidH1 vs vidH2 published_at: lexically vidH1 ("...-04:00") sorts BEFORE
# vidH2 ("...+00:00"), but as instants vidH1 (05:00Z) is LATER than vidH2 (03:00Z)
# — the date-window-uses-instants test depends on this. vidH3 has a NULL
# view_count / ratio (nullable-safety test) and an unparseable published_at
# (window drops it without erroring).
_LIB_VIDEOS = [
    ("vidH1", "Zone 2 base", "chA", "Channel A", "2026-06-08T01:00:00-04:00", 120,
     1, 100000, 12.0, "2026-06-05T09:00:00-04:00", "VO2 max|zone 2 cardio",
     "health", 1, "great"),
    ("vidH2", "Protein myths", "chB", "Channel B", "2026-06-08T03:00:00+00:00", 400,
     0, 5000, 3.0, "2026-06-06T09:00:00-04:00", "high protein",
     "health", 0, ""),
    ("vidH3", "Sleep tips", "chA", "Channel A", "not-a-date", 1500,
     0, None, None, "2026-06-07T09:00:00-04:00", "sleep routine",
     "health", 0, ""),
    ("vidHabit1", "Atomic habits explained", "chC", "Channel C",
     "2026-06-07T12:00:00-04:00", 1000,
     0, 50000, 8.0, "2026-06-04T09:00:00-04:00", "build habits|habit tracker",
     "habit", 1, "interesting"),
    ("vidHabit2", "Habitual", "chB", "Channel B", "2026-06-07T12:00:00-04:00", 200,
     0, 20000, 5.0, "2026-06-04T10:00:00-04:00", "habitual",
     "habit", 0, ""),
]

# (run_date, bucket, rank, video_id). "ghost" in the overall lane has no videos
# row — the LEFT-JOIN-tolerance test asserts it still returns with NULL fields.
_LIB_RANKINGS = [
    ("2026-06-08", "health", 1, "vidH1"),
    ("2026-06-08", "health", 2, "vidH2"),
    ("2026-06-08", "health", 3, "vidH3"),
    ("2026-06-08", "habit", 1, "vidHabit1"),
    ("2026-06-08", "habit", 2, "vidHabit2"),
    ("2026-06-08", "overall", 1, "vidH1"),
    ("2026-06-08", "overall", 2, "vidHabit1"),
    ("2026-06-08", "overall", 3, "ghost"),
]


@pytest.fixture
def library_db_path(tmp_path):
    """A temp DB seeded for the Phase 1 library/filter tests: three channels,
    five videos spanning buckets/countries/durations/dates/star/notes, and
    health/habit/overall rankings (incl. a ranking with no videos row)."""
    db_path = str(tmp_path / "lib.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        conn.executemany(
            "INSERT INTO channels (channel_id, subscriber_count, channel_country) "
            "VALUES (?, ?, ?)",
            _LIB_CHANNELS,
        )
        conn.executemany(
            "INSERT INTO videos (video_id, title, channel_id, channel_title, "
            "published_at, duration_seconds, is_short, view_count, "
            "views_to_subs_ratio, first_seen_at, matched_queries, buckets, "
            "starred, user_notes, link, thumbnail_url) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'http://yt/' || ?, 'http://thumb/' || ?)",
            [v + (v[0], v[0]) for v in _LIB_VIDEOS],
        )
        conn.executemany(
            "INSERT INTO rankings (run_date, bucket, rank, video_id, "
            "metric_value, captured_at) VALUES (?, ?, ?, ?, 1.0, "
            "'2026-06-08T10:00:00-04:00')",
            _LIB_RANKINGS,
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture
def lib_client(library_db_path):
    from fastapi.testclient import TestClient

    from dashboard.app import app, get_db_path

    app.dependency_overrides[get_db_path] = lambda: library_db_path
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# --- Phase 2 (change-over-time charts) fixtures -----------------------------

# stats_snapshots: (run_id, video_id, captured_at, view_count, like_count,
# comment_count). vidA is a multi-point rising series (velocity is computable and
# known: 1000->4000 over exactly one day = 3000 views/day, then 4000->5000 = 1000).
# vidB is a single-point series (no velocity pairs). vidSkew has two snapshots at
# the SAME captured_at (zero delta) so the velocity guard must skip the pair.
_SNAP_ROWS = [
    (1, "vidA", "2026-06-06T10:00:00-04:00", 1000, 10, 1),
    (2, "vidA", "2026-06-07T10:00:00-04:00", 4000, 40, 4),
    (3, "vidA", "2026-06-08T10:00:00-04:00", 5000, 50, 5),
    (1, "vidB", "2026-06-08T10:00:00-04:00", 30000, 100, 9),
    (1, "vidSkew", "2026-06-07T10:00:00-04:00", 100, 1, 0),
    (2, "vidSkew", "2026-06-07T10:00:00-04:00", 500, 5, 0),
]

# videos: (video_id, title, view_count). view_count drives the distribution
# histogram; vidNull's NULL must be excluded from the histogram total.
_CHART_VIDEOS = [
    ("vidA", "Rising star", 150000),
    ("vidB", "New entrant", 30000),
    ("vidFall", "Fell off", 8000),
    ("vidNull", "No views yet", None),
]

# rankings across TWO run_dates in the health lane. vidFall is ranked on 06-07 but
# not 06-08 (the bump-chart 'fell off' gap); vidB and vidNull appear only on 06-08.
_CHART_RANKINGS = [
    ("2026-06-07", "health", 1, "vidA", 9.0),
    ("2026-06-07", "health", 2, "vidFall", 7.0),
    ("2026-06-08", "health", 1, "vidA", 9.5),
    ("2026-06-08", "health", 2, "vidB", 6.0),
    ("2026-06-08", "health", 3, "vidNull", 2.0),
]


@pytest.fixture
def charts_db_path(tmp_path):
    """A temp DB seeded for the Phase 2 chart endpoints: multi-point / single-point
    / zero-delta snapshot series, and a health lane ranked across two run_dates
    with a fell-off video and a NULL-view video."""
    db_path = str(tmp_path / "charts.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        conn.executemany(
            "INSERT INTO stats_snapshots (run_id, video_id, captured_at, "
            "view_count, like_count, comment_count) VALUES (?, ?, ?, ?, ?, ?)",
            _SNAP_ROWS,
        )
        conn.executemany(
            "INSERT INTO videos (video_id, title, view_count, link, thumbnail_url) "
            "VALUES (?, ?, ?, 'http://yt/' || ?, 'http://thumb/' || ?)",
            [(vid, title, vc, vid, vid) for vid, title, vc in _CHART_VIDEOS],
        )
        conn.executemany(
            "INSERT INTO rankings (run_date, bucket, rank, video_id, metric_value, "
            "captured_at) VALUES (?, ?, ?, ?, ?, '2026-06-08T10:00:00-04:00')",
            _CHART_RANKINGS,
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture
def charts_client(charts_db_path):
    from fastapi.testclient import TestClient

    from dashboard.app import app, get_db_path

    app.dependency_overrides[get_db_path] = lambda: charts_db_path
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# --- Phase 4 (spend display) fixtures ---------------------------------------

# llm_invocations: (run_date, scope, model, input_tokens, output_tokens,
# generated_at). Spread across two runs / two Eastern months / a priced and an
# UNPRICED model so the /api/spend tests cover grouping, the run vs month time
# keys, and the null-cost "unavailable" path. "made:up" is deliberately absent
# from config.PRICES.
_SPEND_INVOCATIONS = [
    # run 2026-06-08, June: two openai (GROUP BY collapse) + one anthropic + one
    # unpriced.
    ("2026-06-08", "overall", "openai:gpt-5.4-nano", 3000, 200,
     "2026-06-08T11:00:00-04:00"),
    ("2026-06-08", "health", "openai:gpt-5.4-nano", 1000, 100,
     "2026-06-08T11:05:00-04:00"),
    ("2026-06-08", "habit", "anthropic:claude-haiku-4-5", 2500, 150,
     "2026-06-08T11:06:00-04:00"),
    ("2026-06-08", "overall", "made:up", 4000, 400,
     "2026-06-08T11:07:00-04:00"),
    # run 2026-05-30, May: a DIFFERENT run AND a different month.
    ("2026-05-30", "overall", "openai:gpt-5.4-nano", 500, 50,
     "2026-05-30T22:00:00-04:00"),
]


@pytest.fixture
def spend_db_path(tmp_path):
    """A temp DB seeded only with llm_invocations for the /api/spend tests: two
    runs, two Eastern months, a priced and an unpriced model."""
    db_path = str(tmp_path / "spend.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        conn.executemany(
            "INSERT INTO llm_invocations (run_date, scope, model, input_tokens, "
            "output_tokens, generated_at) VALUES (?, ?, ?, ?, ?, ?)",
            _SPEND_INVOCATIONS,
        )
        # init_db seeded the open price windows with valid_from = today (the log was
        # empty at init); backdate them to the earliest invocation date, exactly as a
        # real migration over an existing log would, so the seeded prices cover this
        # fixture's past-dated invocations. "made:up" has no window and stays unpriced.
        conn.execute(
            "UPDATE model_prices SET valid_from = "
            "(SELECT MIN(substr(generated_at, 1, 10)) FROM llm_invocations) "
            "WHERE valid_to IS NULL"
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture
def spend_client(spend_db_path):
    from fastapi.testclient import TestClient

    from dashboard.app import app, get_db_path

    app.dependency_overrides[get_db_path] = lambda: spend_db_path
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
