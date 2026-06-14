"""Phase 3 price-refresh dashboard endpoints: read, confirm/reject, model, run."""
import pytest

import config
import db
from price_refresh import daily

NOW = "2026-06-13T12:00:00-04:00"


@pytest.fixture
def pr_db_path(tmp_path):
    """init_db seeds a registry + one open price window per model. We add a prior
    window for anthropic (so a delta shows), one pending + one resolved proposal."""
    path = str(tmp_path / "pr.db")
    db.init_db(path)
    conn = db.get_connection(path)
    try:
        # anthropic: a known current window + an earlier prior -> input +11%, output none.
        conn.execute(
            "UPDATE model_prices SET valid_from='2026-06-10', input_per_1m=1.0, "
            "output_per_1m=5.0 WHERE model='anthropic:claude-haiku-4-5' "
            "AND valid_to IS NULL")
        conn.execute(
            "INSERT INTO model_prices (model, input_per_1m, output_per_1m, valid_from, "
            "valid_to, deleted, recorded_at) VALUES "
            "('anthropic:claude-haiku-4-5', 0.9, 5.0, '2026-01-01', '2026-06-10', 0, 'x')")
        # one pending proposal + one already-resolved (must NOT appear in the read).
        conn.execute(
            "INSERT INTO price_proposals (model, field, old_value, new_value, pct_change, "
            "direction, quote, source_url, status, proposed_at) VALUES "
            "('anthropic:claude-haiku-4-5', 'output', 5.0, 9.0, 0.8, 'up', "
            "'Output $9.00 / MTok', 'http://src', 'pending', ?)", (NOW,))
        conn.execute(
            "INSERT INTO price_proposals (model, field, old_value, new_value, pct_change, "
            "direction, quote, source_url, status, proposed_at, resolved_at) VALUES "
            "('openai:gpt-5.4-nano', 'input', 0.2, 0.3, 0.5, 'up', 'q', 'u', "
            "'confirmed', ?, ?)", (NOW, NOW))
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def pr_client(pr_db_path):
    from fastapi.testclient import TestClient

    from dashboard.app import app, get_db_path

    app.dependency_overrides[get_db_path] = lambda: pr_db_path
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# --- GET /api/price-refresh -------------------------------------------------

def test_price_refresh_read_deltas_and_pending_only(pr_client):
    data = pr_client.get("/api/price-refresh").json()
    by_model = {p["model"]: p for p in data["prices"]}
    a = by_model["anthropic:claude-haiku-4-5"]
    assert a["input"]["current"] == 1.0 and a["input"]["prior"] == 0.9
    assert a["input"]["direction"] == "up"
    assert a["output"]["direction"] == "none"            # 5.0 -> 5.0
    # openai has a single seeded window -> no prior data.
    assert by_model["openai:gpt-5.4-nano"]["input"]["prior"] is None
    # proposals: pending only (the confirmed openai row is excluded).
    assert [p["model"] for p in data["proposals"]] == ["anthropic:claude-haiku-4-5"]
    assert data["proposals"][0]["field"] == "output"


# --- confirm / reject -------------------------------------------------------

def _pending_id(pr_client):
    return pr_client.get("/api/price-refresh").json()["proposals"][0]["id"]


def test_confirm_proposal_opens_window_and_clears_pending(pr_client):
    pid = _pending_id(pr_client)
    resp = pr_client.post(f"/api/price-proposals/{pid}/confirm")
    assert resp.status_code == 200 and resp.json()["status"] == "confirmed"
    data = pr_client.get("/api/price-refresh").json()
    assert data["proposals"] == []                       # left pending
    a = {p["model"]: p for p in data["prices"]}["anthropic:claude-haiku-4-5"]
    assert a["output"]["current"] == 9.0                 # staged value applied
    assert a["input"]["current"] == 1.0                  # other field carried forward


def test_reject_proposal_opens_no_window_and_clears_pending(pr_client):
    pid = _pending_id(pr_client)
    resp = pr_client.post(f"/api/price-proposals/{pid}/reject")
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"
    data = pr_client.get("/api/price-refresh").json()
    assert data["proposals"] == []
    a = {p["model"]: p for p in data["prices"]}["anthropic:claude-haiku-4-5"]
    assert a["output"]["current"] == 5.0                 # unchanged (no window opened)


def test_confirm_unknown_proposal_404(pr_client):
    assert pr_client.post("/api/price-proposals/99999/confirm").status_code == 404


# --- get / set extraction model ---------------------------------------------

def test_get_extraction_model_defaults_then_persists(pr_client):
    got = pr_client.get("/api/extraction-model").json()
    assert got["model"] == config.EXTRACTION_MODEL       # settings default, no pref yet
    assert "anthropic:claude-haiku-4-5" in got["options"]

    resp = pr_client.post("/api/extraction-model",
                          json={"model": "ollama:qwen3.5:9b"})
    assert resp.status_code == 200
    assert pr_client.get("/api/extraction-model").json()["model"] == "ollama:qwen3.5:9b"


def test_set_unknown_extraction_model_422_and_no_write(pr_client):
    resp = pr_client.post("/api/extraction-model", json={"model": "nope:nope"})
    assert resp.status_code == 422
    # nothing persisted -> still the default.
    assert pr_client.get("/api/extraction-model").json()["model"] == config.EXTRACTION_MODEL


# --- run --------------------------------------------------------------------

def test_run_returns_summary_with_duration(pr_client, monkeypatch):
    def stub(conn, **kwargs):
        return daily.RunSummary(applied=2, staged=1, rejected=0, skipped=0)
    monkeypatch.setattr(daily, "run_daily", stub)
    resp = pr_client.post("/api/price-refresh/run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] == 2 and body["staged"] == 1
    assert isinstance(body["duration_ms"], int) and body["duration_ms"] >= 0


def test_run_partial_failure_still_200_with_errors(pr_client, monkeypatch):
    def stub(conn, **kwargs):
        return daily.RunSummary(rejected=1, errors=[("x:m", "input", "bad data")])
    monkeypatch.setattr(daily, "run_daily", stub)
    resp = pr_client.post("/api/price-refresh/run")
    assert resp.status_code == 200                        # NOT a 400
    assert resp.json()["rejected"] == 1 and resp.json()["errors"]


def test_run_whole_run_setup_failure_is_clean_400(pr_client, monkeypatch):
    def boom(conn, **kwargs):
        raise ValueError("unknown extraction model nope:nope")
    monkeypatch.setattr(daily, "run_daily", boom)
    resp = pr_client.post("/api/price-refresh/run")
    assert resp.status_code == 400
    assert "unknown extraction model" in resp.json()["detail"]


# --- cross-check provenance payload -----------------------------------------

def _seed_snapshot(path, *, validator_name, source_outcomes, cells):
    conn = db.get_connection(path)
    try:
        def _ins():
            with db.transaction(conn):
                db.insert_cross_check(conn, run_at=NOW, validator_name=validator_name,
                                      source_outcomes=source_outcomes, cells=cells)
        db.run_with_db_retry(_ins)
    finally:
        conn.close()


def test_cross_check_null_before_any_run(pr_client):
    assert pr_client.get("/api/price-refresh").json()["cross_check"] is None


def test_cross_check_payload_parsed_after_a_snapshot(pr_client, pr_db_path):
    _seed_snapshot(
        pr_db_path, validator_name="litellm",
        source_outcomes='[{"role": "validator", "name": "litellm", "ok": true}]',
        cells=('{"anthropic:claude-haiku-4-5": {"input": '
               '{"scraped": 1.0, "validator": 1.0, "flag": "match"}}}'))
    cc = pr_client.get("/api/price-refresh").json()["cross_check"]
    assert cc["validator_name"] == "litellm"
    assert cc["cells"]["anthropic:claude-haiku-4-5"]["input"]["flag"] == "match"
    assert cc["source_outcomes"][0]["role"] == "validator"


def test_run_endpoint_surfaces_latest_cross_check(pr_client, pr_db_path, monkeypatch):
    _seed_snapshot(pr_db_path, validator_name="openrouter",
                   source_outcomes="[]", cells="{}")
    monkeypatch.setattr(daily, "run_daily", lambda conn, **kw: daily.RunSummary())
    body = pr_client.post("/api/price-refresh/run").json()
    assert body["cross_check"]["validator_name"] == "openrouter"
