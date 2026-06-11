import sqlite3
import threading

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
