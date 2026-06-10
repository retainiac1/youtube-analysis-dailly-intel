"""Phase 1 tests: the /api/library ranked-leaderboard browse and the lane-scoped
/api/filter-options endpoint."""

RUN = "2026-06-08"


def _ids(resp):
    """video_ids from a /api/library response, in returned order."""
    return [row["video_id"] for row in resp.json()["rows"]]


def _library(client, lane, **params):
    params = {"run_date": RUN, "lane": lane, **params}
    resp = client.get("/api/library", params=params)
    assert resp.status_code == 200, resp.text
    return resp


# --- Lane scoping -----------------------------------------------------------

def test_lane_scoping_health(lib_client):
    resp = _library(lib_client, "health")
    body = resp.json()
    assert body["run_date"] == RUN
    assert body["lane"] == "health"
    assert body["count"] == 3
    assert _ids(resp) == ["vidH1", "vidH2", "vidH3"]  # ordered by rank
    assert [r["rank"] for r in body["rows"]] == [1, 2, 3]


def test_lane_scoping_habit(lib_client):
    assert _ids(_library(lib_client, "habit")) == ["vidHabit1", "vidHabit2"]


def test_lane_scoping_overall_orders_by_rank(lib_client):
    assert _ids(_library(lib_client, "overall")) == ["vidH1", "vidHabit1", "ghost"]


# --- Delimited matched_queries (split-element, not substring) ----------------

def test_matched_queries_split_element_hit(lib_client):
    resp = _library(lib_client, "habit", matched_query="habit tracker")
    assert _ids(resp) == ["vidHabit1"]


def test_matched_queries_substring_trap_misses(lib_client):
    # "habitual" contains "habit" as a substring but not as a '|' element.
    resp = _library(lib_client, "habit", matched_query="habit")
    assert resp.json()["rows"] == []


def test_matched_queries_multiselect_is_or(lib_client):
    resp = _library(lib_client, "health",
                    matched_query=["high protein", "sleep routine"])
    assert set(_ids(resp)) == {"vidH2", "vidH3"}


# --- Each filter narrows correctly ------------------------------------------

def test_filter_channel(lib_client):
    resp = _library(lib_client, "health", channel_id="chA")
    assert set(_ids(resp)) == {"vidH1", "vidH3"}


def test_filter_country(lib_client):
    resp = _library(lib_client, "health", country="GB")
    assert _ids(resp) == ["vidH2"]


def test_filter_view_range_excludes_nulls(lib_client):
    # vidH2 (5000) below floor; vidH3 (NULL) excluded once a bound is set.
    resp = _library(lib_client, "health", view_min=10000)
    assert _ids(resp) == ["vidH1"]


def test_filter_ratio_range(lib_client):
    resp = _library(lib_client, "health", ratio_min=10)
    assert _ids(resp) == ["vidH1"]


def test_filter_duration_band_short(lib_client):
    assert _ids(_library(lib_client, "health", duration_band="short")) == ["vidH1"]


def test_filter_duration_band_long(lib_client):
    assert _ids(_library(lib_client, "health", duration_band="long")) == ["vidH3"]


def test_filter_starred_only(lib_client):
    assert _ids(_library(lib_client, "health", starred_only=True)) == ["vidH1"]


def test_filter_has_notes_only(lib_client):
    assert _ids(_library(lib_client, "health", has_notes_only=True)) == ["vidH1"]


def test_filter_first_seen_window(lib_client):
    resp = _library(lib_client, "health",
                    first_seen_after="2026-06-06T00:00:00-04:00")
    assert set(_ids(resp)) == {"vidH2", "vidH3"}  # vidH1 first_seen 06-05 excluded


# --- Date window compares instants, not lexical strings ----------------------

def test_published_window_uses_instants(lib_client):
    # 04:00Z is between vidH2 (03:00Z) and vidH1 (05:00Z). Instant comparison
    # keeps vidH1 and drops vidH2 — the opposite of a lexical string compare,
    # which would order "...01:00:00-04:00" before "...03:00:00+00:00".
    # vidH3 ("not-a-date") is unparseable and dropped, not a 500.
    resp = _library(lib_client, "health", published_after="2026-06-08T04:00:00Z")
    assert _ids(resp) == ["vidH1"]


# --- Combined filters AND ----------------------------------------------------

def test_combined_filters_and(lib_client):
    # US + starred: vidH1 (chA/US, starred) qualifies; vidH3 (chA/US, unstarred)
    # fails the star filter; vidH2 (GB) fails the country filter.
    resp = _library(lib_client, "health", country="US", starred_only=True)
    assert _ids(resp) == ["vidH1"]


# --- Empty result is a clean list -------------------------------------------

def test_empty_result_is_clean_list(lib_client):
    resp = _library(lib_client, "health", view_min=99_999_999)
    body = resp.json()
    assert body["count"] == 0
    assert body["rows"] == []


# --- LEFT JOIN tolerance -----------------------------------------------------

def test_left_join_tolerates_missing_video(lib_client):
    rows = _library(lib_client, "overall").json()["rows"]
    ghost = next(r for r in rows if r["video_id"] == "ghost")
    assert ghost["rank"] == 3
    assert ghost["metric_value"] == 1.0
    assert ghost["title"] is None
    assert ghost["channel_country"] is None


# --- Nullable safety ---------------------------------------------------------

def test_null_view_count_included_without_filter(lib_client):
    ids = _ids(_library(lib_client, "health"))
    assert "vidH3" in ids  # NULL view_count present when no view filter set


# --- /api/filter-options is lane-scoped --------------------------------------

def test_filter_options_lane_scoped(lib_client):
    resp = lib_client.get("/api/filter-options",
                          params={"run_date": RUN, "lane": "health"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["matched_queries"] == [
        "VO2 max", "high protein", "sleep routine", "zone 2 cardio",
    ]
    # A value present only in the habit lane must NOT leak in.
    assert "build habits" not in body["matched_queries"]
    chan_ids = {c["channel_id"] for c in body["channels"]}
    assert chan_ids == {"chA", "chB"}  # chC is habit-only
    assert set(body["countries"]) == {"US", "GB"}
    band_keys = {b["key"] for b in body["duration_bands"]}
    assert band_keys == {"short", "mid", "long"}  # no xlong in health lane
    assert body["view_count"] == {"min": 5000, "max": 100000}  # NULL skipped


def test_filter_options_channels_carry_title(lib_client):
    resp = lib_client.get("/api/filter-options",
                          params={"run_date": RUN, "lane": "habit"})
    chans = {c["channel_id"]: c["channel_title"] for c in resp.json()["channels"]}
    assert chans == {"chC": "Channel C", "chB": "Channel B"}


def test_filter_options_empty_lane(lib_client):
    resp = lib_client.get("/api/filter-options",
                          params={"run_date": "2099-01-01", "lane": "health"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["matched_queries"] == []
    assert body["channels"] == []
    assert body["view_count"] == {"min": None, "max": None}


# --- Validation --------------------------------------------------------------

def test_library_missing_run_date_is_422(lib_client):
    assert lib_client.get("/api/library", params={"lane": "health"}).status_code == 422


def test_library_bad_lane_is_422(lib_client):
    resp = lib_client.get("/api/library", params={"run_date": RUN, "lane": "bogus"})
    assert resp.status_code == 422


def test_filter_options_missing_params_is_422(lib_client):
    assert lib_client.get("/api/filter-options",
                          params={"lane": "health"}).status_code == 422
    assert lib_client.get("/api/filter-options",
                          params={"run_date": RUN}).status_code == 422
