"""Phase 2: change-over-time chart endpoints.

Covers /api/snapshots (per-video trajectory + velocity), /api/rank-history (bump
chart across runs), and /api/distribution (view-count histogram). Seeds via the
charts_client fixture (see conftest.py)."""

import config
import db


# --- /api/snapshots ----------------------------------------------------------

def _series(payload, video_id):
    return next(s for s in payload["series"] if s["video_id"] == video_id)


def test_snapshots_multi_point_series_and_velocity(charts_client):
    resp = charts_client.get("/api/snapshots", params={"video_id": "vidA"})
    assert resp.status_code == 200
    s = _series(resp.json(), "vidA")
    # Series carries the video title (charts label by title, not video_id).
    assert s["title"] == "Rising star"
    # Three points, ascending by captured_at.
    assert [p["view_count"] for p in s["points"]] == [1000, 4000, 5000]
    # Velocity has N-1 = 2 points, with the known view/day values.
    assert [v["captured_at"] for v in s["velocity"]] == [
        "2026-06-07T10:00:00-04:00",
        "2026-06-08T10:00:00-04:00",
    ]
    assert [round(v["views_per_day"]) for v in s["velocity"]] == [3000, 1000]


def test_snapshots_single_point_has_empty_velocity(charts_client):
    resp = charts_client.get("/api/snapshots", params={"video_id": "vidB"})
    s = _series(resp.json(), "vidB")
    assert len(s["points"]) == 1
    assert s["velocity"] == []


def test_snapshots_zero_delta_pair_is_skipped(charts_client):
    # vidSkew has two snapshots at the same captured_at; the velocity guard must
    # skip the pair rather than divide by zero.
    resp = charts_client.get("/api/snapshots", params={"video_id": "vidSkew"})
    s = _series(resp.json(), "vidSkew")
    assert len(s["points"]) == 2
    assert s["velocity"] == []


def test_snapshots_absent_video_returns_empty_points(charts_client):
    resp = charts_client.get("/api/snapshots", params={"video_id": "nope"})
    s = _series(resp.json(), "nope")
    assert s["points"] == []
    assert s["velocity"] == []
    assert s["title"] is None


def test_snapshots_empty_video_id_returns_empty_series(charts_client):
    resp = charts_client.get("/api/snapshots")
    assert resp.status_code == 200
    assert resp.json() == {"series": []}


def test_snapshots_over_cap_is_422(charts_client):
    resp = charts_client.get(
        "/api/snapshots",
        params={"video_id": ["a", "b", "c", "d", "e", "f"]},
    )
    assert resp.status_code == 422


def test_snapshots_preserves_requested_order(charts_client):
    resp = charts_client.get(
        "/api/snapshots", params={"video_id": ["vidB", "vidA"]}
    )
    assert [s["video_id"] for s in resp.json()["series"]] == ["vidB", "vidA"]


# --- /api/rank-history -------------------------------------------------------

def test_rank_history_spans_all_run_dates(charts_client):
    resp = charts_client.get("/api/rank-history", params={"lane": "health"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["lane"] == "health"
    assert data["bucket"] == "health"
    # Both run_dates present, ascending — the run picker does not constrain this.
    assert data["run_dates"] == ["2026-06-07", "2026-06-08"]


def test_rank_history_fell_off_video_has_a_gap(charts_client):
    data = charts_client.get(
        "/api/rank-history", params={"lane": "health"}
    ).json()
    by_id = {s["video_id"]: s for s in data["series"]}
    # vidA is ranked both runs; vidFall only on 06-07 (gap on 06-08).
    assert [p["run_date"] for p in by_id["vidA"]["points"]] == [
        "2026-06-07", "2026-06-08",
    ]
    assert [p["run_date"] for p in by_id["vidFall"]["points"]] == ["2026-06-07"]
    # metric_value is the views_to_subs_ratio at that run.
    assert by_id["vidA"]["points"][-1]["metric_value"] == 9.5


def test_rank_history_restricts_to_tracked_videos(charts_client):
    data = charts_client.get(
        "/api/rank-history", params={"lane": "health", "video_id": "vidA"}
    ).json()
    assert [s["video_id"] for s in data["series"]] == ["vidA"]


def test_rank_history_empty_lane_is_clean(charts_client):
    data = charts_client.get(
        "/api/rank-history", params={"lane": "habit"}
    ).json()
    assert data["run_dates"] == []
    assert data["series"] == []


def test_rank_history_bad_lane_is_422(charts_client):
    resp = charts_client.get("/api/rank-history", params={"lane": "bogus"})
    assert resp.status_code == 422


# --- /api/distribution (period-aware, as-of-window) --------------------------
# The histogram now buckets each distinct lane video by its LATEST snapshot at or
# before end_date (not videos.view_count, which is current state). The seed makes the
# distinction visible: vidA has a 06-08 snapshot of 5000 but a current view_count of
# 150000; vidB's only snapshot (30000) is on 06-08; vidFall/vidNull have no snapshots.

def test_distribution_buckets_on_snapshot_not_current_view_count(charts_client):
    resp = charts_client.get(
        "/api/distribution",
        params={"lane": "health", "end_date": "2026-06-08"},
    )
    assert resp.status_code == 200
    data = resp.json()
    # As-of-06-08 snapshots: vidA 5000 (NOT its current 150000), vidB 30000. vidFall
    # and vidNull have no snapshot, so they are omitted.
    expected = config.distribution_buckets([5000, 30000])
    assert {b["label"]: b["count"] for b in data["buckets"]} == expected
    assert [b["label"] for b in data["buckets"]] == list(config.DISTRIBUTION_BUCKETS)
    assert data["total"] == 2
    by_label = {b["label"]: b for b in data["buckets"]}
    # vidA buckets on its in-window snapshot (5-10k), NOT >=100k (its current size).
    assert by_label["5-10k"]["videos"] == [
        {"title": "Rising star", "view_count": 5000}
    ]
    assert by_label[">=100k"]["videos"] == []
    assert by_label["20-50k"]["videos"] == [
        {"title": "New entrant", "view_count": 30000}
    ]


def test_distribution_ceiling_excludes_snapshots_past_end_date(charts_client):
    # A window ending 06-07: vidA keeps getting snapshotted past end_date (its 06-08
    # snapshot of 5000), but it must bucket on the in-window value 4000 (1-5k), not
    # 5000. vidB's only snapshot (06-08) is past the ceiling, so vidB is omitted.
    data = charts_client.get(
        "/api/distribution",
        params={"lane": "health", "end_date": "2026-06-07"},
    ).json()
    assert data["total"] == 1
    by_label = {b["label"]: b for b in data["buckets"]}
    assert by_label["1-5k"]["videos"] == [{"title": "Rising star", "view_count": 4000}]
    assert by_label["5-10k"]["videos"] == []
    assert by_label["20-50k"]["videos"] == []


def test_distribution_single_run_window_uses_that_run_value(charts_client):
    # start=end=06-08 collapses to that run: vidA 5000, vidB 30000 (vidNull no snapshot).
    data = charts_client.get(
        "/api/distribution",
        params={"lane": "health", "start_date": "2026-06-08", "end_date": "2026-06-08"},
    ).json()
    assert data["total"] == 2
    by_label = {b["label"]: b for b in data["buckets"]}
    assert by_label["5-10k"]["videos"] == [{"title": "Rising star", "view_count": 5000}]
    assert by_label["20-50k"]["videos"] == [
        {"title": "New entrant", "view_count": 30000}
    ]


def test_distribution_all_time_uses_absolute_latest_snapshot(charts_client):
    # No dates: no ceiling, so each video's absolute-latest snapshot. Same result as
    # end=06-08 here (06-08 is the newest snapshot), confirming the None-ceiling path.
    data = charts_client.get("/api/distribution", params={"lane": "health"}).json()
    assert data["total"] == 2
    assert {b["label"]: b["count"] for b in data["buckets"]} == (
        config.distribution_buckets([5000, 30000])
    )


def test_distribution_bucket_videos_sorted_desc(charts_client):
    # Within every bucket, view_counts are non-increasing (the endpoint sorts desc).
    data = charts_client.get(
        "/api/distribution",
        params={"lane": "health", "end_date": "2026-06-08"},
    ).json()
    for b in data["buckets"]:
        vcs = [v["view_count"] for v in b["videos"]]
        assert vcs == sorted(vcs, reverse=True)


def test_distribution_empty_lane_is_all_zero(charts_client):
    data = charts_client.get(
        "/api/distribution",
        params={"lane": "habit", "end_date": "2026-06-08"},
    ).json()
    assert data["total"] == 0
    assert all(b["count"] == 0 for b in data["buckets"])


def test_distribution_bad_lane_is_422(charts_client):
    resp = charts_client.get(
        "/api/distribution",
        params={"lane": "bogus", "end_date": "2026-06-08"},
    )
    assert resp.status_code == 422


def test_fetch_distribution_window_picks_latest_by_instant_not_lexical(tmp_path):
    # Two snapshots for one video with DIFFERENT offsets, chosen so lexical order and
    # chronological order DISAGREE: the "+05:00" row has the later calendar date string
    # ("2026-06-09...") but an EARLIER instant (21:00Z 06-08) than the "-04:00" row
    # (03:00Z 06-09). SQL MAX(captured_at) would pick the +05:00 row (222); the
    # instant-correct pick is the -04:00 row (111). Asserts the never-lexical-MAX rule.
    db_path = str(tmp_path / "instant.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO videos (video_id, title, view_count, link) "
            "VALUES ('vidX', 'Offset case', 999, 'http://yt/vidX')"
        )
        conn.executemany(
            "INSERT INTO stats_snapshots (run_id, video_id, captured_at, view_count) "
            "VALUES (?, 'vidX', ?, ?)",
            [
                (1, "2026-06-08T23:00:00-04:00", 111),  # 2026-06-09T03:00Z (later)
                (2, "2026-06-09T02:00:00+05:00", 222),  # 2026-06-08T21:00Z (earlier)
            ],
        )
        conn.execute(
            "INSERT INTO rankings (run_date, bucket, rank, video_id, metric_value, "
            "captured_at) VALUES ('2026-06-08', 'health', 1, 'vidX', 1.0, "
            "'2026-06-08T10:00:00-04:00')"
        )
        conn.commit()
        # All-time (no ceiling): absolute latest by instant is the -04:00 row (111).
        rows = db.fetch_distribution_window(conn, "health", None, None)
        assert rows == [{"video_id": "vidX", "title": "Offset case", "view_count": 111}]
    finally:
        conn.close()
