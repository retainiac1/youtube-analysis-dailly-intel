"""Phase 3 write endpoints: the only mutations the dashboard performs.

Covers the two PUT endpoints (notes, star) end to end via TestClient against the
library fixture: persistence, the user-column-only contract (API-owned columns
untouched), the empty-note / starred_only filter agreement, and 404 on a missing
video. Reuses the lib_client / library_db_path fixtures from conftest.
"""

import db


def _video(db_path, video_id):
    """Read one video row straight from the DB, bypassing the API."""
    conn = db.get_connection(db_path)
    try:
        return conn.execute(
            "SELECT * FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
    finally:
        conn.close()


def _library_ids(client, *, lane="health", **filters):
    params = {"run_date": "2026-06-08", "lane": lane, **filters}
    data = client.get("/api/library", params=params).json()
    return [row["video_id"] for row in data["rows"]]


# --- notes --------------------------------------------------------------------

def test_put_notes_persists_and_preserves_api_columns(lib_client, library_db_path):
    before = _video(library_db_path, "vidH2")

    resp = lib_client.put(
        "/api/videos/vidH2/notes", json={"user_notes": "loved the hook"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"video_id": "vidH2", "user_notes": "loved the hook"}

    after = _video(library_db_path, "vidH2")
    assert after["user_notes"] == "loved the hook"
    # Every API-owned column is byte-for-byte unchanged (the mirror image of the
    # pipeline's user-column contract).
    for col in db.VIDEO_API_COLUMNS:
        assert after[col] == before[col], col
    # Other user columns are untouched too.
    assert after["starred"] == before["starred"]
    assert after["starred_at"] == before["starred_at"]


def test_put_notes_empty_clears_and_drops_from_has_notes(lib_client, library_db_path):
    # vidH1 starts with notes "great" and is in the health lane.
    assert "vidH1" in _library_ids(lib_client, has_notes_only="true")

    resp = lib_client.put("/api/videos/vidH1/notes", json={"user_notes": ""})
    assert resp.status_code == 200
    assert _video(library_db_path, "vidH1")["user_notes"] == ""

    # The write and the has_notes_only read agree that '' means "no note".
    assert "vidH1" not in _library_ids(lib_client, has_notes_only="true")


def test_put_notes_unknown_video_is_404(lib_client):
    resp = lib_client.put("/api/videos/does-not-exist/notes", json={"user_notes": "x"})
    assert resp.status_code == 404


# --- star ---------------------------------------------------------------------

def test_put_star_sets_then_clears(lib_client, library_db_path):
    on = lib_client.put("/api/videos/vidH2/star", json={"starred": True})
    assert on.status_code == 200
    body = on.json()
    assert body["video_id"] == "vidH2"
    assert body["starred"] is True
    assert body["starred_at"]  # a real Eastern ISO timestamp

    row = _video(library_db_path, "vidH2")
    assert row["starred"] == 1
    assert type(row["starred"]) is int  # an int 1/0, never a bool
    assert row["starred_at"] == body["starred_at"]

    off = lib_client.put("/api/videos/vidH2/star", json={"starred": False})
    assert off.status_code == 200
    assert off.json()["starred"] is False
    assert off.json()["starred_at"] is None

    row = _video(library_db_path, "vidH2")
    assert row["starred"] == 0
    assert row["starred_at"] is None


def test_star_change_updates_starred_only_filter(lib_client, library_db_path):
    # vidH1 is seeded starred; unstarring drops it from the starred_only board
    # (the requery-on-star case the frontend relies on).
    assert "vidH1" in _library_ids(lib_client, starred_only="true")

    resp = lib_client.put("/api/videos/vidH1/star", json={"starred": False})
    assert resp.status_code == 200

    assert "vidH1" not in _library_ids(lib_client, starred_only="true")


def test_put_star_unknown_video_is_404(lib_client):
    # "ghost" is ranked (overall lane) but has no videos row, so the UPDATE matches
    # nothing — a 404, never a silent no-op.
    resp = lib_client.put("/api/videos/ghost/star", json={"starred": True})
    assert resp.status_code == 404
