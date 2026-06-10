"""Phase 2: change-over-time chart endpoints.

Covers /api/snapshots (per-video trajectory + velocity), /api/rank-history (bump
chart across runs), and /api/distribution (view-count histogram). Seeds via the
charts_client fixture (see conftest.py)."""

import config


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


# --- /api/distribution -------------------------------------------------------

def test_distribution_matches_shared_buckets(charts_client):
    resp = charts_client.get(
        "/api/distribution",
        params={"run_date": "2026-06-08", "lane": "health"},
    )
    assert resp.status_code == 200
    data = resp.json()
    # Ranked health views on 06-08: vidA 150000, vidB 30000, vidNull NULL (dropped).
    expected = config.distribution_buckets([150000, 30000])
    assert {b["label"]: b["count"] for b in data["buckets"]} == expected
    # Buckets are returned in canonical order, and the NULL view is excluded.
    assert [b["label"] for b in data["buckets"]] == list(
        config.DISTRIBUTION_BUCKETS
    )
    assert data["total"] == 2
    # Each bucket carries the videos behind it (title + view_count), sorted desc.
    by_label = {b["label"]: b for b in data["buckets"]}
    assert by_label[">=100k"]["videos"] == [
        {"title": "Rising star", "view_count": 150000}
    ]
    assert by_label["20-50k"]["videos"] == [
        {"title": "New entrant", "view_count": 30000}
    ]
    # A bucket with no ranked videos carries an empty list (count agrees).
    assert by_label["<1k"]["videos"] == []


def test_distribution_bucket_videos_sorted_desc(charts_client):
    # Two videos land in the same bucket: 22137 and 18680 both in 10-20k? No,
    # 22137 -> 20-50k. Use a bucket we can verify ordering in via the seed: the
    # >=100k bucket has a single video, so assert the general invariant instead:
    # within every bucket, view_counts are non-increasing.
    data = charts_client.get(
        "/api/distribution",
        params={"run_date": "2026-06-08", "lane": "health"},
    ).json()
    for b in data["buckets"]:
        vcs = [v["view_count"] for v in b["videos"]]
        assert vcs == sorted(vcs, reverse=True)


def test_distribution_empty_lane_is_all_zero(charts_client):
    data = charts_client.get(
        "/api/distribution",
        params={"run_date": "2026-06-08", "lane": "habit"},
    ).json()
    assert data["total"] == 0
    assert all(b["count"] == 0 for b in data["buckets"])


def test_distribution_bad_lane_is_422(charts_client):
    resp = charts_client.get(
        "/api/distribution",
        params={"run_date": "2026-06-08", "lane": "bogus"},
    )
    assert resp.status_code == 422
