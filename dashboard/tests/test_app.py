import config


def test_runs_lists_run_dates(client):
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    assert resp.json() == {"run_dates": ["2026-06-08"]}


def test_rankings_returns_lane(client):
    resp = client.get("/api/rankings", params={"run_date": "2026-06-08", "bucket": "health"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_date"] == "2026-06-08"
    assert body["bucket"] == "health"
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["rank"] == 1
    assert row["title"] == "First video"
    assert row["link"] == "http://yt/vid1"
    assert row["view_count"] == 1234


def test_rankings_empty_lane_is_empty_list(client):
    resp = client.get("/api/rankings", params={"run_date": "2026-06-08", "bucket": "habit"})
    assert resp.status_code == 200
    assert resp.json()["rows"] == []


def test_rankings_missing_param_is_422(client):
    assert client.get("/api/rankings", params={"run_date": "2026-06-08"}).status_code == 422
    assert client.get("/api/rankings", params={"bucket": "health"}).status_code == 422


def test_quota_reports_used_and_cap(client):
    resp = client.get("/api/quota")
    assert resp.status_code == 200
    body = resp.json()
    cap = config.DAILY_QUOTA_LIMIT - config.SAFETY_BUFFER
    assert body["pacific_date"] == config.pacific_date()
    assert body["units_used"] == 4200
    assert body["cap"] == cap
    assert body["remaining"] == cap - 4200


def test_interpretation_present(client):
    resp = client.get("/api/interpretation", params={"run_date": "2026-06-08", "scope": "health"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "Looks strong."
    assert body["model"] == "model-x"


def test_interpretation_absent_is_empty_text(client):
    resp = client.get("/api/interpretation", params={"run_date": "2026-06-08", "scope": "habit"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == ""
    assert body["model"] is None


def test_placeholder_page_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "daily-intel dashboard" in resp.text
