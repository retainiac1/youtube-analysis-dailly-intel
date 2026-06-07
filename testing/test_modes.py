import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import config
import db
import swipefile

NOW1 = "2026-06-07T10:00:00-04:00"


# --- fakes ------------------------------------------------------------------

def recent_utc() -> str:
    """A publishedAt within the ranking window (so refreshed/discovered videos
    are eligible regardless of the date the test runs)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fake_video(video_id, channel_id="c1", view_count="200000",
               duration="PT1M", published_at=None):
    return {
        "id": video_id,
        "snippet": {
            "title": "t", "channelId": channel_id, "channelTitle": "ch",
            "publishedAt": published_at or recent_utc(), "description": "d",
            "categoryId": "22", "defaultAudioLanguage": "en",
            "thumbnails": {"high": {"url": "http://x"}}, "tags": ["a"],
        },
        "statistics": {"viewCount": view_count, "likeCount": "10", "commentCount": "2"},
        "contentDetails": {"duration": duration, "definition": "hd", "caption": "true"},
        "status": {"madeForKids": False},
        "topicDetails": {"topicCategories": ["https://en.wikipedia.org/wiki/Health"]},
    }


class _Req:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Endpoint:
    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    def list(self, **kw):
        self.calls.append(kw)
        return _Req(self._handler(kw))


class FakeYouTube:
    """Minimal stand-in for the googleapiclient resource, recording call counts."""

    def __init__(self, *, search_map=None, video_items=None,
                 channel_items=None, comment_items=None):
        search_map = search_map or {}
        video_items = video_items or {}
        channel_items = channel_items or {}
        comment_items = comment_items or {}
        self._search = _Endpoint(lambda kw: {
            "items": [{"id": {"videoId": v}} for v in search_map.get(kw["q"], [])]})
        self._videos = _Endpoint(lambda kw: {
            "items": [video_items[v] for v in kw["id"].split(",") if v in video_items]})
        self._channels = _Endpoint(lambda kw: {
            "items": [channel_items[c] for c in kw["id"].split(",") if c in channel_items]})
        self._comments = _Endpoint(lambda kw: {"items": comment_items.get(kw["videoId"], [])})

    def search(self):
        return self._search

    def videos(self):
        return self._videos

    def channels(self):
        return self._channels

    def commentThreads(self):
        return self._comments


def fake_channel(channel_id="c1", subs="100000"):
    return {
        "id": channel_id,
        "statistics": {"subscriberCount": subs, "videoCount": "10",
                       "viewCount": "1000000"},
        "snippet": {"publishedAt": "2020-01-01T00:00:00Z", "country": "US"},
        "brandingSettings": {"channel": {"keywords": "k"}},
    }


# --- DB seeding helpers -----------------------------------------------------

def make_video_record(video_id="v1", channel_id="c1", **over):
    rec = {
        "video_id": video_id, "title": "Old", "channel_id": channel_id,
        "channel_title": "Chan", "published_at": recent_utc(),
        "duration_seconds": 60, "is_short": 1,
        "link": f"https://youtube.com/shorts/{video_id}",
        "thumbnail_url": "http://t", "description": "old", "category_id": "22",
        "audio_language": "en", "definition": "hd", "has_captions": 1,
        "made_for_kids": 0, "tags": "a", "topic_categories": "Health",
        "top_comments": "orig comment", "matched_queries": "build habits",
        "buckets": "habit", "view_count": 1000, "like_count": 5,
        "comment_count": 1, "views_to_subs_ratio": 0.01, "views_per_day": 100.0,
    }
    rec.update(over)
    return rec


def seed(tmp_path, *, with_video=True):
    db_path = str(tmp_path / "swipe.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    if with_video:
        db.upsert_video(conn, make_video_record(), NOW1)
        db.upsert_channel(conn, {
            "channel_id": "c1", "subscriber_count": 100000,
            "channel_video_count": 10, "channel_total_views": 1000000,
            "channel_created_date": "2020-01-01", "channel_country": "US",
            "channel_keywords": "k"}, NOW1)
        conn.execute("UPDATE videos SET user_notes='mine', starred=1 WHERE video_id='v1'")
        conn.commit()
    conn.close()
    return db_path


def _drive_main(monkeypatch, db_path, argv, youtube=None, queries=None):
    state_path = db_path + ".state.json"
    monkeypatch.setattr(swipefile, "DB_PATH", db_path)
    monkeypatch.setattr(swipefile, "STATE_FILE", state_path)
    monkeypatch.setattr(swipefile, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(swipefile.os, "getenv", lambda *a, **k: "fakekey")
    monkeypatch.setattr(swipefile, "validate_config", lambda *a, **k: None)
    monkeypatch.setattr(swipefile, "build_youtube_client", lambda key: youtube)
    if queries is not None:
        monkeypatch.setattr(swipefile, "SEARCH_QUERIES", queries)
    monkeypatch.setattr(sys, "argv", ["swipefile.py"] + argv)
    swipefile.main()
    return state_path


def latest_run(db_path):
    conn = db.get_connection(db_path)
    try:
        return conn.execute(
            "SELECT * FROM run_log ORDER BY run_id DESC LIMIT 1").fetchone()
    finally:
        conn.close()


# --- refresh branch (direct) ------------------------------------------------

def test_refresh_updates_stats_preserves_user_and_content(tmp_path):
    db_path = seed(tmp_path)
    conn = db.get_connection(db_path)
    try:
        run_id = db.start_run(conn, "refresh", NOW1)
        budget = swipefile.QuotaBudget(0, 9500)
        youtube = FakeYouTube(video_items={"v1": fake_video("v1", view_count="200000")})

        seen, partial = swipefile._run_refresh(youtube, conn, run_id, budget, NOW1)

        assert (seen, partial) == (1, False)
        row = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
        assert row["view_count"] == 200000                 # stat refreshed
        assert row["views_to_subs_ratio"] == 2.0           # 200000/100000 recomputed
        assert row["matched_queries"] == "build habits"    # preserved
        assert row["buckets"] == "habit"                   # preserved
        assert row["top_comments"] == "orig comment"       # preserved
        assert row["user_notes"] == "mine"                 # user column intact
        assert row["starred"] == 1
        # exactly one snapshot for this run
        snaps = conn.execute(
            "SELECT view_count FROM stats_snapshots WHERE run_id=? AND video_id='v1'",
            (run_id,)).fetchall()
        assert [s["view_count"] for s in snaps] == [200000]
        # rankings recomputed
        ranked = conn.execute(
            "SELECT video_id FROM rankings WHERE bucket='overall'").fetchall()
        assert any(r["video_id"] == "v1" for r in ranked)
        # cheap: one videos.list call, no search
        assert len(youtube._videos.calls) == 1
        assert len(youtube._search.calls) == 0
        assert budget.run_units == 1
    finally:
        conn.close()


def test_refresh_no_tracked_videos_is_noop(tmp_path):
    db_path = seed(tmp_path, with_video=False)
    conn = db.get_connection(db_path)
    try:
        run_id = db.start_run(conn, "refresh", NOW1)
        budget = swipefile.QuotaBudget(0, 9500)
        seen, partial = swipefile._run_refresh(None, conn, run_id, budget, NOW1)
        assert (seen, partial) == (0, False)
        assert budget.run_units == 0
    finally:
        conn.close()


def test_near_cap_refresh_guard_stops(tmp_path):
    """A refresh at the cap must guard-stop rather than nick the ceiling."""
    db_path = seed(tmp_path)
    conn = db.get_connection(db_path)
    try:
        run_id = db.start_run(conn, "refresh", NOW1)
        budget = swipefile.QuotaBudget(baseline=9500, cap=9500)  # nothing affordable
        seen, partial = swipefile._run_refresh(None, conn, run_id, budget, NOW1)
        assert budget.guard_stopped is True
        assert seen == 0
        # untouched stat
        row = conn.execute("SELECT view_count FROM videos WHERE video_id='v1'").fetchone()
        assert row["view_count"] == 1000
        assert swipefile._pick_status(budget, False, partial, False) == "quota_guard_stop"
    finally:
        conn.close()


# --- main(): pre-flight, downgrade, mode choice -----------------------------

def test_explicit_discover_preflight_stop(tmp_path, monkeypatch, capsys):
    db_path = seed(tmp_path, with_video=False)
    conn = db.get_connection(db_path)
    today = config.pacific_date()
    db.add_quota_units(conn, today, 9500, NOW1)   # at the cap
    conn.close()

    _drive_main(monkeypatch, db_path, ["--discover"], youtube=None)

    err = capsys.readouterr().err
    assert "PRE-FLIGHT STOP" in err
    run = latest_run(db_path)
    assert run["mode"] == "discover"
    assert run["status"] == "quota_preflight_stop"
    assert run["quota_used"] == 0
    # ledger not advanced by a refused run
    conn = db.get_connection(db_path)
    assert db.get_units_used(conn, today) == 9500
    conn.close()


def test_no_flag_downgrades_to_refresh_when_over_cap(tmp_path, monkeypatch, capsys):
    db_path = seed(tmp_path, with_video=False)
    conn = db.get_connection(db_path)
    today = config.pacific_date()
    db.add_quota_units(conn, today, 9500, NOW1)   # discover can't fit
    conn.close()

    _drive_main(monkeypatch, db_path, [], youtube=None)  # no flag

    err = capsys.readouterr().err
    assert "DOWNGRADE" in err                     # loud, not silent
    run = latest_run(db_path)
    assert run["mode"] == "refresh"
    assert run["status"] == "discover_downgraded_to_refresh"


def test_no_flag_picks_refresh_when_discover_done_today(tmp_path, monkeypatch):
    db_path = seed(tmp_path, with_video=False)
    conn = db.get_connection(db_path)
    # a successful discover already today (Pacific)
    conn.execute(
        "INSERT INTO run_log (mode, started_at, finished_at, quota_used, "
        "videos_seen, status) VALUES ('discover', ?, ?, 200, 0, 'success')",
        (config.now_local_iso(), config.now_local_iso()))
    conn.commit()
    conn.close()

    _drive_main(monkeypatch, db_path, [], youtube=None)

    run = latest_run(db_path)
    assert run["mode"] == "refresh"               # did not re-discover
    conn = db.get_connection(db_path)
    n_discover = conn.execute(
        "SELECT COUNT(*) c FROM run_log WHERE mode='discover'").fetchone()["c"]
    conn.close()
    assert n_discover == 1                         # no new discover run


# --- main(): full discover, eager flush + no double-count -------------------

def test_discover_ledger_equals_run_units_no_double_count(tmp_path, monkeypatch):
    db_path = str(tmp_path / "swipe.db")
    db.init_db(db_path)

    queries = [{"q": "q1", "bucket": "habit"}, {"q": "q2", "bucket": "health"}]
    video_items = {v: fake_video(v) for v in ("v1", "v2", "v3")}  # all channel c1
    youtube = FakeYouTube(
        search_map={"q1": ["v1", "v2"], "q2": ["v2", "v3"]},
        video_items=video_items,
        channel_items={"c1": fake_channel("c1", subs="100000")},
    )

    state_path = _drive_main(monkeypatch, db_path, [], youtube=youtube, queries=queries)

    run = latest_run(db_path)
    assert run["mode"] == "discover"
    assert run["status"] == "success"

    # Cost: 2 search*100 + 2 videos*1 + 1 channels*1 + 3 comments*1 = 206
    expected = 2 * 100 + 2 * 1 + 1 * 1 + 3 * 1
    assert run["quota_used"] == expected

    today = config.pacific_date()
    conn = db.get_connection(db_path)
    try:
        # The ledger total equals run_units exactly: eager search flushes (200) +
        # end-of-run remainder (6), NOT 206 + 200.
        assert db.get_units_used(conn, today) == expected
        assert conn.execute("SELECT COUNT(*) c FROM videos").fetchone()["c"] == 3
        ranked = conn.execute("SELECT COUNT(*) c FROM rankings").fetchone()["c"]
        assert ranked > 0
    finally:
        conn.close()

    # eager-flush call counts: 2 searches, 2 video detail fetches, 1 channel batch
    assert len(youtube._search.calls) == 2
    assert len(youtube._videos.calls) == 2
    assert len(youtube._channels.calls) == 1
    assert not Path(state_path).exists()          # cleared on clean success
