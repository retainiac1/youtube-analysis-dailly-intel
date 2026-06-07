import sqlite3
from datetime import datetime, timezone

import db
import swipefile

# Eastern timestamps (the DB convention) for ranking writes.
NOW = "2026-06-07T14:00:00-04:00"
RUN_DATE = "2026-06-07"


def pool_row(video_id, ratio, buckets="habit", view_count=1000):
    """A minimal ranking-pool row as returned by db.fetch_ranking_pool."""
    return {
        "video_id": video_id,
        "buckets": buckets,
        "views_to_subs_ratio": ratio,
        "view_count": view_count,
    }


# --- compute_rankings: lane selection ----------------------------------------

def test_overall_selection():
    pool = [
        pool_row("a", 5.0),
        pool_row("b", 9.0),
        pool_row("c", 1.0),
    ]
    result = swipefile.compute_rankings(pool, {"health", "habit"}, 20)
    assert result["overall"] == [("b", 9.0), ("a", 5.0), ("c", 1.0)]


def test_bucket_restricted_selection():
    pool = [
        pool_row("habit_only", 8.0, buckets="habit"),
        pool_row("health_only", 7.0, buckets="health"),
        pool_row("both", 9.0, buckets="habit|health"),
    ]
    result = swipefile.compute_rankings(pool, {"health", "habit"}, 20)

    assert [vid for vid, _ in result["habit"]] == ["both", "habit_only"]
    assert [vid for vid, _ in result["health"]] == ["both", "health_only"]
    assert [vid for vid, _ in result["overall"]] == ["both", "habit_only", "health_only"]


def test_exactly_top_n_when_more_exist():
    pool = [pool_row(f"v{i}", float(i)) for i in range(8)]  # ratios 0..7
    result = swipefile.compute_rankings(pool, {"health", "habit"}, 3)
    assert result["overall"] == [("v7", 7.0), ("v6", 6.0), ("v5", 5.0)]


def test_all_returned_when_fewer_than_top_n():
    pool = [pool_row("a", 3.0), pool_row("b", 2.0), pool_row("c", 1.0)]
    result = swipefile.compute_rankings(pool, {"health", "habit"}, 20)
    assert len(result["overall"]) == 3


def test_deterministic_ties():
    # Equal ratio -> view_count desc; equal ratio AND view_count -> video_id asc.
    pool = [
        pool_row("z", 5.0, view_count=100),
        pool_row("a", 5.0, view_count=100),  # ties z on ratio+views -> id asc puts a first
        pool_row("m", 5.0, view_count=999),  # higher views -> first
    ]
    result = swipefile.compute_rankings(pool, {"health", "habit"}, 20)
    assert [vid for vid, _ in result["overall"]] == ["m", "a", "z"]

    # Shuffled input yields identical output (full total order).
    shuffled = [pool[1], pool[2], pool[0]]
    again = swipefile.compute_rankings(shuffled, {"health", "habit"}, 20)
    assert again["overall"] == result["overall"]


def test_empty_lane_returns_empty_list():
    pool = [pool_row("a", 5.0, buckets="habit")]
    result = swipefile.compute_rankings(pool, {"health", "habit"}, 20)
    assert result["health"] == []
    assert [vid for vid, _ in result["habit"]] == ["a"]
    assert [vid for vid, _ in result["overall"]] == ["a"]


def test_null_metric_excluded():
    pool = [
        pool_row("good", 5.0),
        pool_row("hidden", None),
    ]
    result = swipefile.compute_rankings(pool, {"health", "habit"}, 20)
    ids = {vid for vid, _ in result["overall"]}
    assert ids == {"good"}
    assert all("hidden" not in [vid for vid, _ in lane] for lane in result.values())


# --- within_window / eligible_pool: instant-based window filter --------------

CUTOFF = datetime(2026, 6, 4, 0, 0, 0, tzinfo=timezone.utc)


def window_row(video_id, published_at):
    return {"video_id": video_id, "published_at": published_at}


def test_within_window_boundary():
    # Published exactly at the cutoff is inside (>= cutoff); one second before is out.
    assert swipefile.within_window("2026-06-04T00:00:00Z", CUTOFF) is True
    assert swipefile.within_window("2026-06-03T23:59:59Z", CUTOFF) is False


def test_within_window_format_drift():
    # Fractional seconds and an explicit +00:00 offset both represent the cutoff
    # instant and must be treated as inside — these are exactly the cases a naive
    # lexical string >= would wrongly exclude.
    assert swipefile.within_window("2026-06-04T00:00:00.000Z", CUTOFF) is True
    assert swipefile.within_window("2026-06-04T00:00:00+00:00", CUTOFF) is True
    # Sanity: lexical comparison would have gotten these wrong.
    assert "2026-06-04T00:00:00.000Z" < "2026-06-04T00:00:00Z"
    assert "2026-06-04T00:00:00+00:00" < "2026-06-04T00:00:00Z"


def test_eligible_pool_drops_unparseable_published_at():
    rows = [
        window_row("recent", "2026-06-06T00:00:00Z"),
        window_row("garbage", "not-a-date"),
        window_row("empty", ""),
    ]
    kept = swipefile.eligible_pool(rows, CUTOFF)
    assert [r["video_id"] for r in kept] == ["recent"]


def test_eligible_pool_excludes_old_videos():
    rows = [
        window_row("recent", "2026-06-06T00:00:00Z"),
        window_row("old", "2025-01-01T00:00:00Z"),
    ]
    kept = swipefile.eligible_pool(rows, CUTOFF)
    assert [r["video_id"] for r in kept] == ["recent"]


# --- DB integration: fetch_ranking_pool / replace_rankings / _rank_phase -----

def fresh_db(tmp_path) -> sqlite3.Connection:
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    return db.get_connection(db_path)


def insert_video(conn, video_id, *, buckets="habit", ratio=5.0, views=1000,
                 published_at="2026-06-06T00:00:00Z"):
    conn.execute(
        "INSERT INTO videos (video_id, buckets, views_to_subs_ratio, view_count, "
        "published_at) VALUES (?, ?, ?, ?, ?)",
        (video_id, buckets, ratio, views, published_at),
    )
    conn.commit()


def rank_rows(conn, run_date, bucket):
    return conn.execute(
        "SELECT rank, video_id, metric_value, captured_at FROM rankings "
        "WHERE run_date = ? AND bucket = ? ORDER BY rank",
        (run_date, bucket),
    ).fetchall()


def test_replace_rankings_writes_dated_rows(tmp_path):
    conn = fresh_db(tmp_path)
    try:
        with db.transaction(conn):
            db.replace_rankings(conn, RUN_DATE, "habit", [("a", 9.0), ("b", 5.0)], NOW)
        rows = rank_rows(conn, RUN_DATE, "habit")
        assert [(r["rank"], r["video_id"], r["metric_value"]) for r in rows] == [
            (1, "a", 9.0), (2, "b", 5.0),
        ]
        assert all(r["captured_at"] == NOW for r in rows)
    finally:
        conn.close()


def test_idempotent_rerun_replaces_same_day(tmp_path):
    conn = fresh_db(tmp_path)
    try:
        first = [(f"v{i}", float(20 - i)) for i in range(20)]
        with db.transaction(conn):
            db.replace_rankings(conn, RUN_DATE, "habit", first, NOW)

        second = [(f"w{i}", float(12 - i)) for i in range(12)]
        with db.transaction(conn):
            db.replace_rankings(conn, RUN_DATE, "habit", second, NOW)

        rows = rank_rows(conn, RUN_DATE, "habit")
        assert len(rows) == 12                      # ranks 13..20 gone
        assert rows[-1]["rank"] == 12
        assert {r["video_id"] for r in rows} == {f"w{i}" for i in range(12)}
    finally:
        conn.close()


def test_cross_day_history_preserved(tmp_path):
    conn = fresh_db(tmp_path)
    try:
        with db.transaction(conn):
            db.replace_rankings(conn, "2026-06-07", "habit", [("a", 9.0)], NOW)
        with db.transaction(conn):
            db.replace_rankings(conn, "2026-06-08", "habit", [("b", 8.0)], NOW)

        d1 = rank_rows(conn, "2026-06-07", "habit")
        d2 = rank_rows(conn, "2026-06-08", "habit")
        assert [r["video_id"] for r in d1] == ["a"]   # untouched
        assert [r["video_id"] for r in d2] == ["b"]
    finally:
        conn.close()


def test_empty_lane_writes_zero_rows_no_error(tmp_path):
    conn = fresh_db(tmp_path)
    try:
        rankings = {
            "habit": [("a", 9.0)],
            "health": [],
            "overall": [("a", 9.0)],
        }
        db.run_with_db_retry(
            lambda: swipefile._rank_phase(conn, rankings, RUN_DATE, NOW)
        )
        assert len(rank_rows(conn, RUN_DATE, "health")) == 0
        assert len(rank_rows(conn, RUN_DATE, "habit")) == 1
        assert len(rank_rows(conn, RUN_DATE, "overall")) == 1
    finally:
        conn.close()


def test_fetch_ranking_pool_shape(tmp_path):
    conn = fresh_db(tmp_path)
    try:
        insert_video(conn, "v1", buckets="habit|health", ratio=5.0, views=1000)
        pool = db.fetch_ranking_pool(conn)
        assert len(pool) == 1
        row = pool[0]
        assert isinstance(row, dict) and not isinstance(row, sqlite3.Row)
        assert {"video_id", "buckets", "views_to_subs_ratio", "view_count",
                "published_at"} <= set(row)
    finally:
        conn.close()
