import sqlite3
import threading

import pytest

import config
import db
import interpret
import llm


def _query_in_thread(conn):
    """Run a trivial query on `conn` from a fresh thread; return the exception
    raised there (or None). sqlite3 raises ProgrammingError when a connection is
    used off its creating thread and check_same_thread is True."""
    box = {}

    def run():
        try:
            conn.execute("SELECT 1").fetchone()
            box["err"] = None
        except Exception as e:  # noqa: BLE001 - we want to inspect it
            box["err"] = e

    t = threading.Thread(target=run)
    t.start()
    t.join()
    return box["err"]


def test_get_connection_default_blocks_cross_thread(tmp_path):
    """The pipeline contract: the default connection is single-thread guarded."""
    path = str(tmp_path / "t.db")
    db.init_db(path)
    conn = db.get_connection(path)
    try:
        assert isinstance(_query_in_thread(conn), sqlite3.ProgrammingError)
    finally:
        conn.close()


def test_get_connection_check_same_thread_false_allows_cross_thread(tmp_path):
    """The dashboard opens connections with check_same_thread=False so FastAPI's
    threadpool can create the connection on one worker and use it on another
    (concurrent chart requests) without a ProgrammingError."""
    path = str(tmp_path / "t.db")
    db.init_db(path)
    conn = db.get_connection(path, check_same_thread=False)
    try:
        assert _query_in_thread(conn) is None
    finally:
        conn.close()


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


def test_interpretation_full_shape(client):
    """Populated row returns every field the Interpretation page renders."""
    resp = client.get(
        "/api/interpretation", params={"run_date": "2026-06-08", "scope": "health"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_date"] == "2026-06-08"
    assert body["scope"] == "health"
    assert body["text"] == "Looks strong."
    assert body["model"] == "model-x"
    assert body["generated_at"] == "2026-06-08T10:05:00-04:00"
    # Seeded interpretation has no llm_invocations row, so no measured run time.
    assert body["duration_ms"] is None


def test_interpretation_absent_scope_is_clean_empty(client):
    """A scope with no row (overall) returns the clean empty contract, not an
    error: 200 with empty text and null metadata."""
    resp = client.get(
        "/api/interpretation", params={"run_date": "2026-06-08", "scope": "overall"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == ""
    assert body["model"] is None
    assert body["generated_at"] is None


def test_interpretation_scope_keys_the_lookup(client):
    """Same run, different scope: health has a row, habit does not. The page sends
    scope=<active lane>, so the populated panel only appears for the lane with a
    row; this guards that scope (not just run_date) selects the interpretation."""
    health = client.get(
        "/api/interpretation", params={"run_date": "2026-06-08", "scope": "health"}).json()
    habit = client.get(
        "/api/interpretation", params={"run_date": "2026-06-08", "scope": "habit"}).json()
    assert health["text"] == "Looks strong."
    assert habit["text"] == ""


# --- Phase 3 generator: POST /api/interpret + GET /api/interpret-defaults ---
# The generator core does `from llm import generate`, so the mock seam is
# `interpret.generate` (the name bound in interpret's namespace), not `llm.generate`.
# Mocking it exercises the real endpoint -> synthesize_lane -> DB write path while
# never touching the network. The seeded `health` lane is non-empty; `habit` is
# empty; the seeded log has NO llm_invocations rows.

VALID_MODEL = "openai:gpt-5.4-nano"


def _fake_generate(text="Synthesized summary.", input_tokens=100, output_tokens=30):
    def fake(model, prompt, *, temperature, seed):
        return llm.GenerateResult(text, input_tokens, output_tokens, seed_applied=seed)
    return fake


def _count(db_path, table, where="", params=()):
    conn = db.get_connection(db_path)
    try:
        return conn.execute(
            f"SELECT count(*) AS n FROM {table} {where}", params
        ).fetchone()["n"]
    finally:
        conn.close()


def test_interpret_persists_and_logs(client, seeded_db_path, monkeypatch):
    monkeypatch.setattr(interpret, "generate", _fake_generate())
    resp = client.post("/api/interpret", json={
        "run_date": "2026-06-08", "scope": "health",
        "model": VALID_MODEL, "temperature": 0.7, "seed": 7,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["skipped"] is False
    assert body["text"] == "Synthesized summary."
    assert body["input_tokens"] == 100 and body["output_tokens"] == 30
    assert body["seed_applied"] == 7
    assert body["model"] == VALID_MODEL  # echoed for immediate card render
    # The measured run time is returned and persisted.
    assert isinstance(body["duration_ms"], int) and body["duration_ms"] >= 0

    # One interpretation row for (health) — overwritten, still one — and one new
    # invocation row carrying the posted params.
    assert _count(seeded_db_path, "interpretations",
                  "WHERE run_date=? AND scope=?", ("2026-06-08", "health")) == 1
    inv = db.get_connection(seeded_db_path)
    try:
        row = inv.execute(
            "SELECT model, temperature, seed, input_tokens, output_tokens, "
            "duration_ms FROM llm_invocations ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        inv.close()
    assert row["model"] == VALID_MODEL
    assert row["temperature"] == 0.7 and row["seed"] == 7
    assert row["input_tokens"] == 100 and row["output_tokens"] == 30
    assert row["duration_ms"] == body["duration_ms"]  # logged == returned
    # The stored text + run time are now readable via the existing read endpoint.
    read = client.get("/api/interpretation",
                      params={"run_date": "2026-06-08", "scope": "health"}).json()
    assert read["text"] == "Synthesized summary."
    assert read["duration_ms"] == body["duration_ms"]


def test_interpret_empty_lane_skips(client, seeded_db_path, monkeypatch):
    called = []
    monkeypatch.setattr(interpret, "generate",
                        lambda *a, **k: called.append(1))
    resp = client.post("/api/interpret", json={
        "run_date": "2026-06-08", "scope": "habit",  # no rankings -> empty lane
        "model": VALID_MODEL, "temperature": 1.0, "seed": None,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["skipped"] is True
    assert "model" not in body and "text" not in body
    assert called == []  # the LLM seam was never invoked
    assert _count(seeded_db_path, "interpretations",
                  "WHERE scope=?", ("habit",)) == 0
    assert _count(seeded_db_path, "llm_invocations") == 0


def test_interpret_llm_error_is_clean_400(client, monkeypatch):
    def boom(*a, **k):
        raise llm.LLMError("missing OPENAI_API_KEY for openai")
    monkeypatch.setattr(interpret, "generate", boom)
    resp = client.post("/api/interpret", json={
        "run_date": "2026-06-08", "scope": "health",
        "model": VALID_MODEL, "temperature": 0.7, "seed": None,
    })
    assert resp.status_code == 400
    assert "OPENAI_API_KEY" in resp.json()["detail"]


def test_interpret_provider_api_error_is_clean_400(client, monkeypatch):
    # A provider API error (the adapter wraps it as LLMError) must surface as a
    # clean 400 with the message, NOT a raw 500 — the grok-seed-0 class of failure.
    def boom(*a, **k):
        raise llm.LLMError("xai: Error code: 400 - Seed must be positive but seed = 0")
    monkeypatch.setattr(interpret, "generate", boom)
    resp = client.post("/api/interpret", json={
        "run_date": "2026-06-08", "scope": "health",
        "model": VALID_MODEL, "temperature": 0.7, "seed": 0,
    })
    assert resp.status_code == 400
    assert "Seed must be positive" in resp.json()["detail"]


def test_interpret_bad_model_is_422(client, monkeypatch):
    # Validation rejects the model before any generate call.
    def _fail_if_called(*a, **k):
        raise AssertionError("generate must not run when the model is invalid")
    monkeypatch.setattr(interpret, "generate", _fail_if_called)
    resp = client.post("/api/interpret", json={
        "run_date": "2026-06-08", "scope": "health",
        "model": "bogus:x", "temperature": 0.7, "seed": None,
    })
    assert resp.status_code == 422


def test_interpret_bad_scope_is_422(client):
    resp = client.post("/api/interpret", json={
        "run_date": "2026-06-08", "scope": "nope",
        "model": VALID_MODEL, "temperature": 0.7, "seed": None,
    })
    assert resp.status_code == 422


def test_interpret_defaults_empty_table(client):
    body = client.get("/api/interpret-defaults").json()
    assert body["models"], "no models offered"
    # Sorted allowed set -> first is anthropic.
    assert body["model"] == "anthropic:claude-haiku-4-5"
    assert body["temperature"] == interpret.DEFAULT_TEMPERATURE
    assert body["seed"] is None
    caps = body["capabilities"]
    assert caps["anthropic:claude-haiku-4-5"]["seed"] is False
    assert caps["openai:gpt-5.4-nano"]["temperature"] is False
    # Prompt-field options (all) + the selection (default when nothing persisted).
    keys = [f["key"] for f in body["available_fields"]]
    assert "like_count" in keys and "top_comments" in keys
    assert body["selected_fields"] == interpret.DEFAULT_PROMPT_FIELDS


def test_interpret_persists_field_selection(client, seeded_db_path, monkeypatch):
    monkeypatch.setattr(interpret, "generate", _fake_generate())
    # Send an unordered list with an unknown key; it is normalized on the way in.
    client.post("/api/interpret", json={
        "run_date": "2026-06-08", "scope": "health", "model": VALID_MODEL,
        "temperature": 0.7, "seed": None,
        "fields": ["like_count", "view_count", "bogus"],
    })
    # The persisted selection is the normalized set, and the defaults read echoes it.
    body = client.get("/api/interpret-defaults").json()
    assert body["selected_fields"] == ["view_count", "like_count"]
    stored = _count(seeded_db_path, "app_preferences",
                    "WHERE key='prompt_fields'")
    assert stored == 1


def test_interpret_defaults_with_history(client, seeded_db_path):
    # Insert one invocation inline (do NOT pollute the shared fixture); the defaults
    # endpoint must echo its model/temperature/seed as the last-used values.
    conn = db.get_connection(seeded_db_path)
    try:
        with db.transaction(conn):
            db.log_invocation(
                conn, "2026-06-08", "health", "xai:grok-4-fast",
                0.9, 123, None, 500, 60, "2026-06-08T12:00:00-04:00",
            )
    finally:
        conn.close()
    body = client.get("/api/interpret-defaults").json()
    assert body["model"] == "xai:grok-4-fast"
    assert body["temperature"] == 0.9
    assert body["seed"] == 123


def test_placeholder_page_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "daily-intel dashboard" in resp.text


def test_index_asset_links_resolve(client):
    """Every CSS/JS the page references must actually serve. Guards against a
    wrong path (the static mount is at '/', so links must NOT carry a '/static'
    prefix)."""
    import re

    page = client.get("/").text
    assets = re.findall(r'(?:href|src)="(/[^"]+\.(?:css|js))"', page)
    assert assets, "index.html references no local css/js"
    for url in assets:
        assert client.get(url).status_code == 200, f"asset 404: {url}"


# --- Phase 4: /api/spend ----------------------------------------------------


def _by_model(section):
    return {m["model"]: m for m in section["per_model"]}


def test_spend_run_and_month_sections(spend_client, monkeypatch):
    """The run section sums the selected run; the month section sums the current
    Eastern month. The two use different time keys, so the May invocation is in
    neither when the run is June-08 and "now" is June."""
    monkeypatch.setattr(config, "now_local_iso", lambda: "2026-06-11T09:00:00-04:00")
    body = spend_client.get("/api/spend", params={"run_date": "2026-06-08"}).json()
    assert body["run_date"] == "2026-06-08"
    assert body["month"] == "2026-06"

    run = _by_model(body["run"])
    # openai: two June-08 invocations summed (3000+1000 / 200+100).
    assert run["openai:gpt-5.4-nano"]["input_tokens"] == 4000
    assert run["openai:gpt-5.4-nano"]["output_tokens"] == 300
    assert run["openai:gpt-5.4-nano"]["invocations"] == 2
    expected = llm.estimate_cost("openai:gpt-5.4-nano", 4000, 300, config.PRICES)
    assert run["openai:gpt-5.4-nano"]["cost"] == pytest.approx(expected)
    assert run["anthropic:claude-haiku-4-5"]["input_tokens"] == 2500

    # Month-to-date over June: same three models as the June-08 run (the only run
    # in June here), NOT the May openai row.
    month = _by_model(body["month_to_date"])
    assert month["openai:gpt-5.4-nano"]["input_tokens"] == 4000  # 500 May excluded
    assert "made:up" in month


def test_spend_unpriced_model_reports_tokens_null_cost(spend_client, monkeypatch):
    monkeypatch.setattr(config, "now_local_iso", lambda: "2026-06-11T09:00:00-04:00")
    body = spend_client.get("/api/spend", params={"run_date": "2026-06-08"}).json()
    run = _by_model(body["run"])
    # "made:up" is absent from PRICES: tokens still report, cost is null.
    assert run["made:up"]["input_tokens"] == 4000
    assert run["made:up"]["cost"] is None
    assert body["run"]["has_unpriced"] is True
    # The unpriced row is excluded from the labeled total (= sum of priced only).
    priced = (llm.estimate_cost("openai:gpt-5.4-nano", 4000, 300, config.PRICES)
              + llm.estimate_cost("anthropic:claude-haiku-4-5", 2500, 150,
                                  config.PRICES))
    assert body["run"]["total_cost"] == pytest.approx(priced)


def test_spend_recomputes_on_price_change(spend_client, monkeypatch):
    """Cost is computed at display, not stored: changing PRICES changes the
    returned total for the SAME stored rows."""
    monkeypatch.setattr(config, "now_local_iso", lambda: "2026-06-11T09:00:00-04:00")
    monkeypatch.setattr(
        config, "PRICES", {"openai:gpt-5.4-nano": {"input": 1.0, "output": 5.0}}
    )
    first = spend_client.get(
        "/api/spend", params={"run_date": "2026-06-08"}
    ).json()["run"]["total_cost"]

    monkeypatch.setattr(
        config, "PRICES", {"openai:gpt-5.4-nano": {"input": 2.0, "output": 10.0}}
    )
    second = spend_client.get(
        "/api/spend", params={"run_date": "2026-06-08"}
    ).json()["run"]["total_cost"]

    assert second == pytest.approx(first * 2)


def test_spend_month_boundary_uses_generated_at(spend_client, monkeypatch):
    """Patching "now" to May moves the month window: only the May invocation
    (generated_at 2026-05-30) lands in month_to_date."""
    monkeypatch.setattr(config, "now_local_iso", lambda: "2026-05-31T09:00:00-04:00")
    body = spend_client.get("/api/spend").json()
    assert body["month"] == "2026-05"
    month = _by_model(body["month_to_date"])
    assert list(month) == ["openai:gpt-5.4-nano"]
    assert month["openai:gpt-5.4-nano"]["input_tokens"] == 500
    assert month["openai:gpt-5.4-nano"]["invocations"] == 1


def test_spend_without_run_date_empties_run_section(spend_client, monkeypatch):
    monkeypatch.setattr(config, "now_local_iso", lambda: "2026-06-11T09:00:00-04:00")
    body = spend_client.get("/api/spend").json()
    assert body["run_date"] is None
    assert body["run"]["per_model"] == []
    assert body["run"]["total_cost"] == 0.0
    # Month-to-date still computes.
    assert body["month_to_date"]["per_model"]


def test_spend_empty_log_returns_zeroed(client, monkeypatch):
    """The default seeded DB has no llm_invocations: zeroed sections, status 200."""
    monkeypatch.setattr(config, "now_local_iso", lambda: "2026-06-11T09:00:00-04:00")
    resp = client.get("/api/spend", params={"run_date": "2026-06-08"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["run"]["per_model"] == []
    assert body["run"]["total_cost"] == 0.0
    assert body["run"]["has_unpriced"] is False
    assert body["month_to_date"]["per_model"] == []
