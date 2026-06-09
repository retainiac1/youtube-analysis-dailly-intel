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
            "INSERT INTO interpretations (run_date, scope, text, model, "
            "generated_at) VALUES ('2026-06-08', 'health', 'Looks strong.', "
            "'model-x', '2026-06-08T10:05:00-04:00')"
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
