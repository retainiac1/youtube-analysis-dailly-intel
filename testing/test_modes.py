import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httplib2
import pytest
from googleapiclient.errors import HttpError

import config
import db
import llm
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


def _patch_main(monkeypatch, db_path, argv, youtube=None, queries=None):
    """Wire up main()'s external deps against a temp DB and fake YouTube. Returns
    the state-file path. Callers then invoke swipefile.main() themselves."""
    state_path = db_path + ".state.json"
    monkeypatch.setattr(swipefile, "DB_PATH", db_path)
    monkeypatch.setattr(swipefile, "STATE_FILE", state_path)
    monkeypatch.setattr(swipefile, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(swipefile.os, "getenv", lambda *a, **k: "fakekey")
    monkeypatch.setattr(swipefile, "validate_config", lambda *a, **k: None)
    # These exercise quota/ledger/run_date through a full discover; the English/no-kids
    # LLM phase is orthogonal and would make a real network call here. Disable it (the
    # real skip path); classification has its own tests (test_classify*.py).
    monkeypatch.setattr(config, "CLASSIFICATION_ENABLED", False)
    monkeypatch.setattr(swipefile, "build_youtube_client", lambda key: youtube)
    if queries is not None:
        monkeypatch.setattr(swipefile, "SEARCH_QUERIES", queries)
    monkeypatch.setattr(sys, "argv", ["swipefile.py"] + argv)
    return state_path


def _drive_main(monkeypatch, db_path, argv, youtube=None, queries=None):
    state_path = _patch_main(monkeypatch, db_path, argv, youtube, queries)
    swipefile.main()
    return state_path


def _drive_main_code(monkeypatch, db_path, argv, youtube=None, queries=None):
    """Like _drive_main but returns main()'s exit code (the contract under test)."""
    _patch_main(monkeypatch, db_path, argv, youtube, queries)
    return swipefile.main()


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

        seen, partial = swipefile._run_refresh(youtube, conn, run_id, budget, NOW1, NOW1[:10])

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
        seen, partial = swipefile._run_refresh(None, conn, run_id, budget, NOW1, NOW1[:10])
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
        seen, partial = swipefile._run_refresh(None, conn, run_id, budget, NOW1, NOW1[:10])
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


# --- main(): exit codes (scheduler retry contract) --------------------------

def test_argparse_usage_error_exits_other(tmp_path, monkeypatch):
    """A bad flag must NOT exit 2 (that would read as EXIT_NETWORK); argparse's
    SystemExit(2) is remapped to EXIT_OTHER(5)."""
    db_path = seed(tmp_path, with_video=False)
    assert _drive_main_code(monkeypatch, db_path, ["--bogus"]) == config.EXIT_OTHER


def test_missing_api_key_exits_auth(tmp_path, monkeypatch):
    db_path = seed(tmp_path, with_video=False)
    _patch_main(monkeypatch, db_path, [], youtube=None)
    monkeypatch.setattr(swipefile.os, "getenv", lambda *a, **k: "")  # no key
    assert swipefile.main() == config.EXIT_AUTH


def test_dry_run_exits_ok_not_no_rows(tmp_path, monkeypatch):
    """--dry-run writes nothing (0 rankings) but must exit OK, never EXIT_NO_ROWS:
    it is a terminal branch that skips the clean-run classification."""
    db_path = seed(tmp_path, with_video=False)
    assert _drive_main_code(monkeypatch, db_path, ["--dry-run"]) == config.EXIT_OK


def test_explicit_discover_preflight_exits_quota(tmp_path, monkeypatch):
    db_path = seed(tmp_path, with_video=False)
    conn = db.get_connection(db_path)
    db.add_quota_units(conn, config.pacific_date(), 9500, NOW1)  # at the cap
    conn.close()
    assert _drive_main_code(monkeypatch, db_path, ["--discover"]) == config.EXIT_QUOTA


def test_discover_with_zero_results_exits_no_rows(tmp_path, monkeypatch):
    """A clean discover that captures nothing (empty searches -> zero rankings)
    exits EXIT_NO_ROWS(7)."""
    db_path = str(tmp_path / "swipe.db")
    db.init_db(db_path)
    queries = [{"q": "q1", "bucket": "habit"}]
    youtube = FakeYouTube(search_map={"q1": []})  # searches return nothing
    code = _drive_main_code(monkeypatch, db_path, ["--discover"], youtube=youtube,
                            queries=queries)
    assert code == config.EXIT_NO_ROWS
    assert latest_run(db_path)["status"] == "success"


def _keep_gen_result():
    return SimpleNamespace(
        text=json.dumps({"english": True, "kids_targeted": False,
                         "kids_subject": False, "reason": "r"}),
        input_tokens=10, output_tokens=5)


def test_discover_fallback_budget_breaker_exits_classify_budget(tmp_path, monkeypatch):
    """End-to-end: the primary classifier is throttled (429) so every video fails
    over to the paid fallback; once the per-run cap is hit the run aborts with
    EXIT_CLASSIFY_BUDGET (8), distinct from a YouTube quota stop, and the survivors
    classified so far are persisted (status classify_budget_stop)."""
    N = 2
    vids = [f"v{i}" for i in range(N + 1)]
    youtube = FakeYouTube(
        search_map={"q1": vids},
        video_items={v: fake_video(v) for v in vids},
        channel_items={"c1": fake_channel()},
    )
    db_path = str(tmp_path / "swipe.db")
    db.init_db(db_path)
    queries = [{"q": "q1", "bucket": "habit"}]
    _patch_main(monkeypatch, db_path, ["--discover"], youtube=youtube, queries=queries)
    monkeypatch.setattr(config, "CLASSIFICATION_ENABLED", True)
    monkeypatch.setattr(config, "CLASSIFICATION_RETRY",
                        {**config.CLASSIFICATION_RETRY, "max_retries": 0,
                         "max_fallback_videos": N})

    # The primary passes its preflight probe but 429s every real classification; the
    # fallback always succeeds. Branch on the preflight marker in the prompt so the
    # primary is alive at preflight (in model_order) yet throttled per video.
    def make_gen(conn, model, run_date):
        provider = model.split(":", 1)[0]
        def gen(prompt):
            if provider == "google" and "preflight" not in prompt:
                raise llm.LLMError("throttled", status_code=429)
            return _keep_gen_result()
        return gen

    monkeypatch.setattr(swipefile, "_make_classify_generate", make_gen)
    code = swipefile.main()
    assert code == config.EXIT_CLASSIFY_BUDGET
    assert latest_run(db_path)["status"] == "classify_budget_stop"
    # The N classified-and-kept survivors were persisted; the tripping one was not.
    conn = db.get_connection(db_path)
    persisted = conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"]
    conn.close()
    assert persisted == N


def test_refresh_with_empty_pool_exits_ok_not_no_rows(tmp_path, monkeypatch):
    """A clean refresh whose ranking pool ages out (zero rankings written through
    _recompute_rankings at :863) is NORMAL, not a failure: EXIT_OK, never
    EXIT_NO_ROWS. Guards the auto-mode daily false-alarm."""
    old = "2000-01-01T00:00:00Z"  # far outside the ranking window
    db_path = str(tmp_path / "swipe.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    db.upsert_video(conn, make_video_record(published_at=old), NOW1)
    db.upsert_channel(conn, {
        "channel_id": "c1", "subscriber_count": 100000, "channel_video_count": 10,
        "channel_total_views": 1000000, "channel_created_date": "2020-01-01",
        "channel_country": "US", "channel_keywords": "k"}, NOW1)
    conn.commit()
    conn.close()
    youtube = FakeYouTube(video_items={"v1": fake_video("v1", published_at=old)})

    code = _drive_main_code(monkeypatch, db_path, ["--refresh"], youtube=youtube)

    assert code == config.EXIT_OK
    n = db.count_rankings_for_run_date(
        db.get_connection(db_path), config.now_local_iso()[:10])
    assert n == 0  # the pool aged out, yet we exit OK


@pytest.mark.parametrize("reason,status,expected", [
    ("quotaExceeded", 403, config.EXIT_QUOTA),
    ("network", None, config.EXIT_NETWORK),
    ("keyInvalid", None, config.EXIT_AUTH),
    ("badRequest", 400, config.EXIT_OTHER),
])
def test_pipeline_api_error_is_classified_and_run_marked_failed(
        tmp_path, monkeypatch, reason, status, expected):
    """A PipelineApiError raised mid-work classifies to the mapped code, the run is
    recorded 'failed' (never falsely success), and the resume cache is preserved."""
    db_path = seed(tmp_path, with_video=False)

    def boom(*a, **k):
        raise swipefile.PipelineApiError(reason, status)

    state_path = _patch_main(monkeypatch, db_path, ["--discover"], youtube=object())
    monkeypatch.setattr(swipefile, "_run_discover", boom)
    Path(state_path).write_text("{}")  # a resume cache exists

    assert swipefile.main() == expected
    assert latest_run(db_path)["status"] == "failed"
    assert Path(state_path).exists()  # not cleared on abort


def test_failing_finally_preserves_db_locked_code(tmp_path, monkeypatch):
    """A DB-locked abort that ALSO makes finish_run raise in the finally must still
    exit EXIT_DB_LOCKED(6), never a generic 1: the decided code survives a failing
    finally."""
    db_path = seed(tmp_path, with_video=False)

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    _patch_main(monkeypatch, db_path, ["--discover"], youtube=object())
    monkeypatch.setattr(swipefile, "_run_discover", locked)
    monkeypatch.setattr(db, "finish_run", locked)  # finally also raises

    assert swipefile.main() == config.EXIT_DB_LOCKED


def test_run_date_single_capture_survives_midnight_straddle(tmp_path, monkeypatch):
    """run_date is captured ONCE at run start and threaded to both the rankings
    write and the count. If wall-clock crosses midnight mid-run, rows stay stamped
    under the start date and the count finds them, so a clean discover exits OK,
    not a false EXIT_NO_ROWS."""
    day1 = "2026-06-07T23:59:30-04:00"
    day2 = "2026-06-08T00:00:30-04:00"
    seq = [day1]  # first call (run_started) is day1; every later call is day2

    def clock():
        return seq.pop(0) if seq else day2

    monkeypatch.setattr(swipefile, "now_local_iso", clock)

    db_path = str(tmp_path / "swipe.db")
    db.init_db(db_path)
    queries = [{"q": "q1", "bucket": "habit"}]
    youtube = FakeYouTube(
        search_map={"q1": ["v1"]},
        video_items={"v1": fake_video("v1")},
        channel_items={"c1": fake_channel("c1")},
    )
    code = _drive_main_code(monkeypatch, db_path, ["--discover"], youtube=youtube,
                            queries=queries)

    assert code == config.EXIT_OK
    conn = db.get_connection(db_path)
    dates = [r["run_date"] for r in
             conn.execute("SELECT DISTINCT run_date FROM rankings").fetchall()]
    conn.close()
    assert dates == ["2026-06-07"]  # stamped under the run-start date, not day2


# --- QUOTA_STOP_STATUSES pinned to its producers ----------------------------

def test_quota_stop_statuses_pinned_to_producers():
    """The set must equal EXACTLY the strings the producers emit. If a producer is
    renamed without updating the set, that quota stop would fall back to the
    retryable default and retry a real daily cap all day."""
    assert config.QUOTA_STOP_STATUSES == frozenset(
        {"quota_guard_stop", "quota_exceeded", "quota_preflight_stop"})

    guard = swipefile.QuotaBudget(0, 100)
    guard.guard_stopped = True
    assert swipefile._pick_status(guard, False, False, False) == "quota_guard_stop"
    assert "quota_guard_stop" in config.QUOTA_STOP_STATUSES

    plain = swipefile.QuotaBudget(0, 100)
    assert swipefile._pick_status(plain, True, False, False) == "quota_exceeded"
    assert "quota_exceeded" in config.QUOTA_STOP_STATUSES
    # quota_preflight_stop is written by the pre-flight path in main(); pinned here
    # so a rename there is caught too.
    assert "quota_preflight_stop" in config.QUOTA_STOP_STATUSES


# --- api_call_with_retry: terminal failures RAISE, benign skips return None --

def _http_error(status, reason):
    """A real googleapiclient HttpError with the given status and error reason."""
    err = HttpError(httplib2.Response({"status": status}), b"{}")
    err.error_details = [{"reason": reason}] if reason else []
    err.resp.status = status
    return err


def _afford_budget():
    return swipefile.QuotaBudget(0, 1000)


def test_choke_point_quota_raises_without_retry(monkeypatch):
    monkeypatch.setattr(swipefile.time, "sleep", lambda *_: None)
    calls = []

    def call_fn():
        calls.append(1)
        raise _http_error(403, "quotaExceeded")

    with pytest.raises(swipefile.PipelineApiError) as ei:
        swipefile.api_call_with_retry(call_fn, _afford_budget(), 1)
    assert (ei.value.reason, ei.value.status) == ("quotaExceeded", 403)
    assert len(calls) == 1  # no same-day retry


def test_choke_point_key_invalid_raises_auth_signal(monkeypatch):
    monkeypatch.setattr(swipefile.time, "sleep", lambda *_: None)

    def call_fn():
        raise _http_error(400, "keyInvalid")

    with pytest.raises(swipefile.PipelineApiError) as ei:
        swipefile.api_call_with_retry(call_fn, _afford_budget(), 1)
    assert config.exit_code_for_api_failure(ei.value.reason, ei.value.status) == config.EXIT_AUTH


def test_choke_point_bare_400_raises_other_signal(monkeypatch):
    monkeypatch.setattr(swipefile.time, "sleep", lambda *_: None)

    def call_fn():
        raise _http_error(400, "")  # badRequest, no reason

    with pytest.raises(swipefile.PipelineApiError) as ei:
        swipefile.api_call_with_retry(call_fn, _afford_budget(), 1)
    assert config.exit_code_for_api_failure(ei.value.reason, ei.value.status) == config.EXIT_OTHER


def test_choke_point_benign_per_item_returns_none(monkeypatch):
    """A private video / comments-off must NOT abort the run: still a None skip."""
    monkeypatch.setattr(swipefile.time, "sleep", lambda *_: None)
    for reason in ("commentsDisabled", "forbidden", "videoNotFound"):
        def call_fn(r=reason):
            raise _http_error(403, r)
        assert swipefile.api_call_with_retry(call_fn, _afford_budget(), 1) is None


def test_choke_point_network_retries_then_raises(monkeypatch):
    monkeypatch.setattr(swipefile.time, "sleep", lambda *_: None)
    calls = []

    def call_fn():
        calls.append(1)
        raise ConnectionError("boom")

    with pytest.raises(swipefile.PipelineApiError) as ei:
        swipefile.api_call_with_retry(call_fn, _afford_budget(), 1)
    assert config.exit_code_for_api_failure(ei.value.reason, ei.value.status) == config.EXIT_NETWORK
    assert len(calls) == 2  # one in-function retry, then raise


def test_choke_point_dns_failure_raises_network(monkeypatch):
    """A DNS lookup failure surfaces as httplib2.ServerNotFoundError, which is NOT
    an OSError. It must still classify as EXIT_NETWORK, not EXIT_OTHER."""
    monkeypatch.setattr(swipefile.time, "sleep", lambda *_: None)

    def call_fn():
        raise swipefile.httplib2.ServerNotFoundError("Unable to find the server")

    with pytest.raises(swipefile.PipelineApiError) as ei:
        swipefile.api_call_with_retry(call_fn, _afford_budget(), 1)
    assert config.exit_code_for_api_failure(ei.value.reason, ei.value.status) == config.EXIT_NETWORK


def test_choke_point_rate_limit_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(swipefile.time, "sleep", lambda *_: None)
    calls = []

    def call_fn():
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429, "")
        return {"ok": True}

    assert swipefile.api_call_with_retry(call_fn, _afford_budget(), 1) == {"ok": True}
    assert len(calls) == 2  # transient retry recovered, no raise
