import sqlite3

import pytest

import db
import swipefile

NOW1 = "2026-06-07T10:00:00-04:00"
NOW2 = "2026-06-07T12:00:00-04:00"
NOW3 = "2026-06-07T14:00:00-04:00"


def make_video_record(video_id: str = "vid1", **overrides) -> dict:
    """A complete videos record (video_id + every VIDEO_API_COLUMNS key)."""
    record = {
        "video_id": video_id,
        "title": "Title", "channel_id": "chan1", "channel_title": "Chan",
        "published_at": "2026-06-05T00:00:00Z", "duration_seconds": 45,
        "is_short": 1, "link": "https://youtube.com/shorts/vid1",
        "thumbnail_url": "http://thumb", "description": "desc",
        "category_id": "22", "audio_language": "en", "definition": "hd",
        "has_captions": 1, "made_for_kids": 0, "tags": "a|b",
        "topic_categories": "Health", "top_comments": "c", "matched_queries": "build habits",
        "buckets": "habit", "view_count": 1000, "like_count": 50, "comment_count": 5,
        "views_to_subs_ratio": 10.0, "views_per_day": 500.0,
    }
    record.update(overrides)
    return record


def fresh_db(tmp_path) -> sqlite3.Connection:
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    return db.get_connection(db_path)


def upsert(conn, record, now):
    db.upsert_video(conn, record, now)
    conn.commit()


def fetch(conn, video_id="vid1"):
    return conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()


# --- user-column contract -----------------------------------------------------

def test_user_columns_preserved_against_stats_change(tmp_path):
    conn = fresh_db(tmp_path)
    try:
        upsert(conn, make_video_record(view_count=1000), NOW1)
        conn.execute(
            "UPDATE videos SET user_notes='mine', starred=1, starred_at=?, hook='my hook' "
            "WHERE video_id='vid1'", (NOW1,),
        )
        conn.commit()

        upsert(conn, make_video_record(view_count=9999), NOW2)  # stats changed

        row = fetch(conn)
        assert row["view_count"] == 9999          # API field refreshed
        assert row["user_notes"] == "mine"        # user fields untouched
        assert row["starred"] == 1
        assert row["starred_at"] == NOW1
        assert row["hook"] == "my hook"
    finally:
        conn.close()


def test_user_columns_preserved_against_buckets_and_matched_queries_change(tmp_path):
    """buckets and matched_queries are API-owned and in the UPDATE SET; refreshing
    them must NOT touch user columns. This is the exact clobber the contract
    exists to prevent."""
    conn = fresh_db(tmp_path)
    try:
        upsert(conn, make_video_record(buckets="habit", matched_queries="build habits"), NOW1)
        conn.execute(
            "UPDATE videos SET user_notes='keep me', starred=1, hook='h', "
            "first_10_sec='f', saves='s' WHERE video_id='vid1'"
        )
        conn.commit()

        # Second run: the video now also matched a health query.
        upsert(conn, make_video_record(
            buckets="habit|health", matched_queries="build habits|zone 2 cardio"), NOW2)

        row = fetch(conn)
        assert row["buckets"] == "habit|health"                     # refreshed
        assert row["matched_queries"] == "build habits|zone 2 cardio"  # refreshed
        assert row["user_notes"] == "keep me"                       # untouched
        assert row["starred"] == 1
        assert row["hook"] == "h"
        assert row["first_10_sec"] == "f"
        assert row["saves"] == "s"
    finally:
        conn.close()


def test_first_seen_at_set_once(tmp_path):
    conn = fresh_db(tmp_path)
    try:
        upsert(conn, make_video_record(view_count=1000), NOW1)
        assert fetch(conn)["first_seen_at"] == NOW1
        upsert(conn, make_video_record(view_count=2000), NOW2)  # later, changed
        assert fetch(conn)["first_seen_at"] == NOW1             # unchanged
    finally:
        conn.close()


def test_last_api_refresh_at_moves_only_on_real_change(tmp_path):
    conn = fresh_db(tmp_path)
    try:
        upsert(conn, make_video_record(view_count=1000), NOW1)
        assert fetch(conn)["last_api_refresh_at"] == NOW1

        upsert(conn, make_video_record(view_count=1000), NOW2)  # identical API fields
        assert fetch(conn)["last_api_refresh_at"] == NOW1       # not advanced

        upsert(conn, make_video_record(view_count=1234), NOW3)  # one field changed
        assert fetch(conn)["last_api_refresh_at"] == NOW3       # advanced
    finally:
        conn.close()


def test_idempotent_single_row(tmp_path):
    conn = fresh_db(tmp_path)
    try:
        upsert(conn, make_video_record(), NOW1)
        upsert(conn, make_video_record(), NOW1)  # identical re-run
        count = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        assert count == 1
    finally:
        conn.close()


# --- snapshots ----------------------------------------------------------------

def test_snapshots_one_per_run(tmp_path):
    conn = fresh_db(tmp_path)
    try:
        db.insert_snapshot(conn, 1, "vid1", NOW1, 1000, 50, 5)
        db.insert_snapshot(conn, 1, "vid1", NOW1, 1000, 50, 5)  # same run: no-op
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM stats_snapshots").fetchone()[0] == 1

        db.insert_snapshot(conn, 2, "vid1", NOW2, 2000, 60, 6)  # new run: new row
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM stats_snapshots").fetchone()[0] == 2
    finally:
        conn.close()


# --- run_log ------------------------------------------------------------------

def test_run_log_start_and_finish(tmp_path):
    conn = fresh_db(tmp_path)
    try:
        run_id = db.start_run(conn, "discover", NOW1)
        assert isinstance(run_id, int)
        row = conn.execute("SELECT * FROM run_log WHERE run_id=?", (run_id,)).fetchone()
        assert row["status"] == "running" and row["finished_at"] is None

        db.finish_run(conn, run_id, NOW2, 217, 12, "success")
        row = conn.execute("SELECT * FROM run_log WHERE run_id=?", (run_id,)).fetchone()
        assert row["status"] == "success"
        assert row["finished_at"] == NOW2
        assert row["quota_used"] == 217
        assert row["videos_seen"] == 12
    finally:
        conn.close()


# --- categories ---------------------------------------------------------------

def test_persist_categories_upserts_and_refreshes(tmp_path):
    """persist_categories inserts one row per category and re-running refreshes the
    title/region in place (no duplicates) while advancing last_updated_at."""
    conn = fresh_db(tmp_path)
    try:
        swipefile.persist_categories(conn, [
            {"category_id": "26", "title": "Howto & Style", "region_code": "US"},
            {"category_id": "24", "title": "Entertainment", "region_code": "US"},
        ], NOW1)

        rows = conn.execute(
            "SELECT category_id, title, region_code, last_updated_at "
            "FROM categories ORDER BY category_id"
        ).fetchall()
        assert [r["category_id"] for r in rows] == ["24", "26"]
        assert {r["title"] for r in rows} == {"Entertainment", "Howto & Style"}
        assert all(r["region_code"] == "US" and r["last_updated_at"] == NOW1 for r in rows)

        # Re-run with a changed title + region: upsert in place, advance timestamp.
        swipefile.persist_categories(conn, [
            {"category_id": "26", "title": "How-to & Style", "region_code": "GB"},
        ], NOW2)

        assert conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 2
        row = conn.execute(
            "SELECT * FROM categories WHERE category_id='26'").fetchone()
        assert row["title"] == "How-to & Style"
        assert row["region_code"] == "GB"
        assert row["last_updated_at"] == NOW2
    finally:
        conn.close()


# --- DB-locked retry ----------------------------------------------------------

def test_db_locked_retries_then_succeeds():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert db.run_with_db_retry(fn, sleep=lambda *_: None) == "ok"
    assert calls["n"] == 2


def test_non_lock_operational_error_is_reraised():
    def fn():
        raise sqlite3.OperationalError("no such table: videos")

    with pytest.raises(sqlite3.OperationalError):
        db.run_with_db_retry(fn, sleep=lambda *_: None)


# --- builders -----------------------------------------------------------------

def test_bucket_union():
    q_to_bucket = {"build habits": "habit", "zone 2 cardio": "health"}
    assert swipefile.bucket_union(["build habits", "zone 2 cardio"], q_to_bucket) == "habit|health"
    assert swipefile.bucket_union(["build habits"], q_to_bucket) == "habit"
    assert swipefile.bucket_union([], q_to_bucket) == ""


def fake_video(video_id="v1", duration="PT3M", view_count="1000",
               published_at="2026-06-05T00:00:00Z", **stats):
    statistics = {}
    if view_count is not None:
        statistics["viewCount"] = view_count
    statistics.update(stats)
    return {
        "id": video_id,
        "snippet": {
            "title": "t", "channelId": "c1", "channelTitle": "ch",
            "publishedAt": published_at, "description": "d", "categoryId": "22",
            "defaultAudioLanguage": "en", "thumbnails": {"high": {"url": "http://x"}},
            "tags": ["a", "b"],
        },
        "statistics": statistics,
        "contentDetails": {"duration": duration, "definition": "hd", "caption": "true"},
        "status": {"madeForKids": False},
        "topicDetails": {"topicCategories": ["https://en.wikipedia.org/wiki/Health"]},
    }


def test_is_short_threshold():
    ch = {"subscriber_count": "100"}
    at = swipefile.build_video_record(fake_video(duration="PT3M"), [], "habit", ch, "")
    over = swipefile.build_video_record(fake_video(duration="PT3M1S"), [], "habit", ch, "")
    assert at["is_short"] == 1      # 180s == SHORT_MAX_SECONDS
    assert over["is_short"] == 0    # 181s


def test_metric_guards_do_not_raise():
    # zero subs and a publish date of today must not raise.
    rec = swipefile.build_video_record(
        fake_video(published_at="2026-06-07T00:00:00Z"), ["build habits"], "habit",
        {"subscriber_count": "0"}, "")
    assert rec["views_to_subs_ratio"] is not None
    assert rec["views_per_day"] is not None


def test_missing_view_count_yields_none():
    rec = swipefile.build_video_record(
        fake_video(view_count=None), [], "habit", {"subscriber_count": "100"}, "")
    assert rec["view_count"] is None
    assert rec["views_to_subs_ratio"] is None
    assert rec["views_per_day"] is None
