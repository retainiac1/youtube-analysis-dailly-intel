"""Phase 2 — the /models editor endpoints. Mirrors test_writes.py: TestClient over
a seeded DB (the `client` + `seeded_db_path` fixtures), POST/PUT json, assert
status + echo + read DB state directly. The seeded DB has the baseline models
(each with one open price window dated "today")."""
import sqlite3

import config
import db


def test_dashboard_startup_self_heals_unmigrated_db(tmp_path, monkeypatch):
    """Launching the dashboard against an un-migrated (bare) DB must not 500: the
    startup lifespan runs init_db, so the registry endpoints work. This is the exact
    failure a standalone dashboard launch over a v6 DB hit."""
    from fastapi.testclient import TestClient
    from dashboard.app import app

    bare = str(tmp_path / "bare.db")
    sqlite3.connect(bare).close()                 # a v0 file with no schema
    monkeypatch.setenv("DASHBOARD_DB_PATH", bare)
    # The `with` form triggers the lifespan (startup migrate + seed).
    with TestClient(app) as client:
        resp = client.get("/api/models")
        assert resp.status_code == 200
        assert {m["model"] for m in resp.json()["models"]} == {
            "anthropic:claude-haiku-4-5", "openai:gpt-5.4-nano",
            "xai:grok-4-fast", "google:gemini-2.5-flash-lite",
            "ollama:qwen3.5:9b",
        }


def _model(db_path, model):
    conn = db.get_connection(db_path)
    try:
        return conn.execute("SELECT * FROM models WHERE model = ?", (model,)).fetchone()
    finally:
        conn.close()


def _prices(db_path, model):
    conn = db.get_connection(db_path)
    try:
        return conn.execute(
            "SELECT * FROM model_prices WHERE model = ? ORDER BY valid_from", (model,)
        ).fetchall()
    finally:
        conn.close()


def _add_invocation(db_path, model, generated_at, *, run_date="r",
                    inp=1_000_000, out=0):
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO llm_invocations (run_date, scope, model, input_tokens, "
                "output_tokens, generated_at) VALUES (?, 'overall', ?, ?, ?, ?)",
                (run_date, model, inp, out, generated_at),
            )
    finally:
        conn.close()


def _dropdown(client):
    return client.get("/api/interpret-defaults").json()["models"]


# --- GET /api/models --------------------------------------------------------

def test_get_models_includes_supported_providers(client):
    """The page populates the provider picker from this list, so it must be the ONE
    backend constant (no hardcoded JS list that drifts when a provider is added)."""
    import llm
    body = client.get("/api/models").json()
    assert body["providers"] == list(llm.SUPPORTED_PROVIDERS)


def test_post_model_empty_id_is_422(client):
    """A bare 'provider:' (empty model id) fails closed, not an empty-id insert."""
    resp = client.post("/api/models", json={
        "model": "anthropic:", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": False,
    })
    assert resp.status_code == 422


def test_get_models_lists_seeded_with_and_without_deleted(client, seeded_db_path):
    body = client.get("/api/models").json()
    models = {m["model"] for m in body["models"]}
    assert models == {
        "anthropic:claude-haiku-4-5", "openai:gpt-5.4-nano",
        "xai:grok-4-fast", "google:gemini-2.5-flash-lite",
        "ollama:qwen3.5:9b",
    }
    # Soft-delete one; default read hides it, include_deleted surfaces it.
    assert client.post("/api/models/xai:grok-4-fast/delete").status_code == 200
    live = {m["model"] for m in client.get("/api/models").json()["models"]}
    assert "xai:grok-4-fast" not in live
    allm = {m["model"] for m in
            client.get("/api/models", params={"include_deleted": "true"}).json()["models"]}
    assert "xai:grok-4-fast" in allm


# --- PUT /api/models/{model} ------------------------------------------------

def test_put_model_edits_flags_and_notes(client, seeded_db_path):
    resp = client.put("/api/models/openai:gpt-5.4-nano", json={
        "enabled": False, "supports_temperature": True, "supports_seed": False,
        "is_reasoning": False, "notes": "edited", "max_tokens": 2048,
    })
    assert resp.status_code == 200
    row = _model(seeded_db_path, "openai:gpt-5.4-nano")
    assert row["enabled"] == 0 and row["supports_temperature"] == 1
    assert row["supports_seed"] == 0 and row["notes"] == "edited"


def test_put_model_unknown_is_404(client):
    resp = client.put("/api/models/nope:x", json={
        "enabled": True, "supports_temperature": True, "supports_seed": True,
        "is_reasoning": False, "notes": None, "max_tokens": 1024,
    })
    assert resp.status_code == 404


def test_put_model_is_pk_locked(client, seeded_db_path):
    """A `model` in the body is ignored — the PK comes only from the path and is
    immutable. The path row updates; no row appears under the body's value."""
    resp = client.put("/api/models/openai:gpt-5.4-nano", json={
        "model": "hacked:x",  # extra field, must be ignored
        "enabled": True, "supports_temperature": True, "supports_seed": True,
        "is_reasoning": False, "notes": "still nano", "max_tokens": 2048,
    })
    assert resp.status_code == 200
    assert _model(seeded_db_path, "hacked:x") is None          # PK not editable
    assert _model(seeded_db_path, "openai:gpt-5.4-nano")["notes"] == "still nano"


# --- POST /api/models -------------------------------------------------------

def test_post_model_inserts(client, seeded_db_path):
    resp = client.post("/api/models", json={
        "model": "openai:gpt-4o", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": False, "notes": "added",
    })
    assert resp.status_code == 200
    row = _model(seeded_db_path, "openai:gpt-4o")
    assert row is not None
    assert row["provider"] == "openai"   # derived, not from the client
    assert row["enabled"] == 1 and row["supports_temperature"] == 1


def test_post_ollama_model_double_colon_inserts(client, seeded_db_path):
    # The add-form path for a NEW ollama tag with a double-colon id: provider derived
    # via the first-colon split, the rest of the id (its own ':') preserved.
    resp = client.post("/api/models", json={
        "model": "ollama:mistral:7b", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": True, "notes": "local",
    })
    assert resp.status_code == 200
    row = _model(seeded_db_path, "ollama:mistral:7b")
    assert row is not None
    assert row["provider"] == "ollama"   # derived from the stored string, not client
    assert row["is_reasoning"] == 1


def test_get_models_provider_picker_includes_ollama(client):
    # The provider picker is fed from the one backend constant, so it now offers
    # ollama for the add form (no second JS list to drift).
    body = client.get("/api/models").json()
    assert "ollama" in body["providers"]


def test_post_model_bad_string_is_422(client):
    resp = client.post("/api/models", json={
        "model": "noprovider", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": False,
    })
    assert resp.status_code == 422


def test_post_model_unsupported_provider_is_422(client):
    resp = client.post("/api/models", json={
        "model": "cohere:command", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": False,
    })
    assert resp.status_code == 422


def test_post_model_duplicate_is_409(client):
    resp = client.post("/api/models", json={
        "model": "anthropic:claude-haiku-4-5", "supports_temperature": True,
        "supports_seed": False, "is_reasoning": False,
    })
    assert resp.status_code == 409


# --- model dropdown / spend interplay ---------------------------------------

def test_new_model_offered_only_after_a_price_is_added(client, seeded_db_path):
    client.post("/api/models", json={
        "model": "openai:gpt-4o", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": False,
    })
    assert "openai:gpt-4o" not in _dropdown(client)   # no price yet -> not offered
    resp = client.post("/api/models/openai:gpt-4o/prices", json={
        "valid_from": "2026-01-01", "input_per_1m": 0.5, "output_per_1m": 1.5,
    })
    assert resp.status_code == 200
    assert "openai:gpt-4o" in _dropdown(client)        # now offered


def test_delete_model_hides_from_dropdown_but_spend_still_prices(client, seeded_db_path):
    # A custom model with an explicit early window + a past invocation.
    client.post("/api/models", json={
        "model": "openai:gpt-4o", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": False,
    })
    client.post("/api/models/openai:gpt-4o/prices", json={
        "valid_from": "2026-01-01", "input_per_1m": 1.0, "output_per_1m": 0.0,
    })
    _add_invocation(seeded_db_path, "openai:gpt-4o", "2026-05-01T12:00:00-04:00",
                    run_date="gpt4o-run")
    assert "openai:gpt-4o" in _dropdown(client)

    assert client.post("/api/models/openai:gpt-4o/delete").status_code == 200
    assert "openai:gpt-4o" not in _dropdown(client)    # gone from dropdown
    # ...but its past run still prices in spend.
    run = client.get("/api/spend", params={"run_date": "gpt4o-run"}).json()["run"]
    by = {m["model"]: m for m in run["per_model"]}
    assert by["openai:gpt-4o"]["cost"] is not None and by["openai:gpt-4o"]["cost"] > 0

    assert client.post("/api/models/openai:gpt-4o/restore").status_code == 200
    assert "openai:gpt-4o" in _dropdown(client)         # restored


def test_zero_price_run_is_free_and_additive(client, seeded_db_path):
    # A $0/$0 window prices an invocation to EXACTLY 0.0 (not None, not an error),
    # and is additive: a $0 local run alongside a priced cloud run totals the cloud
    # cost only, with no zeroing or corruption of the total.
    client.post("/api/models", json={
        "model": "ollama:free-tag", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": True,
    })
    client.post("/api/models/ollama:free-tag/prices", json={
        "valid_from": "2026-01-01", "input_per_1m": 0.0, "output_per_1m": 0.0,
    })
    client.post("/api/models", json={
        "model": "openai:gpt-4o", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": False,
    })
    client.post("/api/models/openai:gpt-4o/prices", json={
        "valid_from": "2026-01-01", "input_per_1m": 1.0, "output_per_1m": 0.0,
    })
    # 1M input tokens each, same run, same month.
    _add_invocation(seeded_db_path, "ollama:free-tag", "2026-05-01T12:00:00-04:00",
                    run_date="mix", inp=1_000_000, out=500_000)
    _add_invocation(seeded_db_path, "openai:gpt-4o", "2026-05-01T12:00:00-04:00",
                    run_date="mix", inp=1_000_000, out=0)

    run = client.get("/api/spend", params={"run_date": "mix"}).json()["run"]
    by = {m["model"]: m for m in run["per_model"]}
    assert by["ollama:free-tag"]["cost"] == 0.0          # exactly 0, even with tokens
    assert by["openai:gpt-4o"]["cost"] == 1.0            # 1M input * $1/M
    total = sum(m["cost"] for m in run["per_model"])
    assert total == 1.0                                  # additive: $0 did not corrupt


# --- POST /api/models/{model}/prices ----------------------------------------

def test_post_price_auto_closes_prior_window(client, seeded_db_path):
    client.post("/api/models", json={
        "model": "openai:gpt-4o", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": False,
    })
    client.post("/api/models/openai:gpt-4o/prices", json={
        "valid_from": "2026-01-01", "input_per_1m": 1.0, "output_per_1m": 2.0,
    })
    resp = client.post("/api/models/openai:gpt-4o/prices", json={
        "valid_from": "2026-06-01", "input_per_1m": 3.0, "output_per_1m": 4.0,
    })
    assert resp.status_code == 200
    rows = {r["valid_from"]: r for r in _prices(seeded_db_path, "openai:gpt-4o")}
    assert rows["2026-01-01"]["valid_to"] == "2026-06-01"   # auto-closed
    assert rows["2026-06-01"]["valid_to"] is None           # new open


def test_post_price_negative_is_422(client, seeded_db_path):
    resp = client.post("/api/models/openai:gpt-5.4-nano/prices", json={
        "valid_from": "2030-01-01", "input_per_1m": -1.0, "output_per_1m": 1.0,
    })
    assert resp.status_code == 422


def test_post_price_bad_date_is_422(client, seeded_db_path):
    resp = client.post("/api/models/openai:gpt-5.4-nano/prices", json={
        "valid_from": "june first", "input_per_1m": 1.0, "output_per_1m": 1.0,
    })
    assert resp.status_code == 422


def test_post_price_overlap_is_422(client, seeded_db_path):
    client.post("/api/models", json={
        "model": "openai:gpt-4o", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": False,
    })
    client.post("/api/models/openai:gpt-4o/prices", json={
        "valid_from": "2026-06-01", "input_per_1m": 1.0, "output_per_1m": 2.0,
    })
    # valid_from not strictly after the latest window's start -> 422.
    resp = client.post("/api/models/openai:gpt-4o/prices", json={
        "valid_from": "2026-05-01", "input_per_1m": 3.0, "output_per_1m": 4.0,
    })
    assert resp.status_code == 422


def test_post_price_unknown_model_is_404(client):
    resp = client.post("/api/models/nope:x/prices", json={
        "valid_from": "2026-06-01", "input_per_1m": 1.0, "output_per_1m": 1.0,
    })
    assert resp.status_code == 404


def test_delete_open_price_then_readd_no_double_count(client, seeded_db_path):
    """End-to-end deleted-agnostic auto-close: deleting the open price then adding a
    new one must NOT leave two open windows (spend would double-count)."""
    client.post("/api/models", json={
        "model": "openai:gpt-4o", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": False,
    })
    p = client.post("/api/models/openai:gpt-4o/prices", json={
        "valid_from": "2026-01-01", "input_per_1m": 1.0, "output_per_1m": 0.0,
    }).json()
    _add_invocation(seeded_db_path, "openai:gpt-4o", "2026-09-01T12:00:00-04:00",
                    run_date="dd-run")
    # Soft-delete the open window, then add a new one.
    assert client.post(f"/api/prices/{p['id']}/delete").status_code == 200
    assert client.post("/api/models/openai:gpt-4o/prices", json={
        "valid_from": "2026-06-01", "input_per_1m": 2.0, "output_per_1m": 0.0,
    }).status_code == 200
    run = client.get("/api/spend", params={"run_date": "dd-run"}).json()["run"]
    by = {m["model"]: m for m in run["per_model"]}
    assert by["openai:gpt-4o"]["invocations"] == 1          # not 2 (no double-count)
    assert by["openai:gpt-4o"]["input_tokens"] == 1_000_000  # not doubled
    assert by["openai:gpt-4o"]["cost"] == 2.0               # only the new window


def test_price_delete_and_restore_still_prices(client, seeded_db_path):
    client.post("/api/models", json={
        "model": "openai:gpt-4o", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": False,
    })
    p = client.post("/api/models/openai:gpt-4o/prices", json={
        "valid_from": "2026-01-01", "input_per_1m": 5.0, "output_per_1m": 0.0,
    }).json()
    _add_invocation(seeded_db_path, "openai:gpt-4o", "2026-05-01T12:00:00-04:00",
                    run_date="pr-run")
    assert client.post(f"/api/prices/{p['id']}/delete").status_code == 200
    # Deleted window still prices in spend.
    run = client.get("/api/spend", params={"run_date": "pr-run"}).json()["run"]
    by = {m["model"]: m for m in run["per_model"]}
    assert by["openai:gpt-4o"]["cost"] == 5.0
    assert client.post(f"/api/prices/{p['id']}/restore").status_code == 200
    assert _prices(seeded_db_path, "openai:gpt-4o")[0]["deleted"] == 0


# --- count reads ------------------------------------------------------------

def test_invocation_count_reads(client, seeded_db_path):
    client.post("/api/models", json={
        "model": "openai:gpt-4o", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": False,
    })
    pid = client.post("/api/models/openai:gpt-4o/prices", json={
        "valid_from": "2026-01-01", "input_per_1m": 1.0, "output_per_1m": 0.0,
    }).json()["id"]
    _add_invocation(seeded_db_path, "openai:gpt-4o", "2026-02-15T12:00:00-05:00")
    _add_invocation(seeded_db_path, "openai:gpt-4o", "2026-03-15T12:00:00-04:00")
    assert client.get("/api/models/openai:gpt-4o/invocation-count").json()["count"] == 2
    # The open window covers both.
    assert client.get(f"/api/prices/{pid}/invocation-count").json()["count"] == 2


# --- max_tokens (Phase 2): per-model output cap, editable + validated --------
# Policy: a cloud/paid model MUST have a positive int <= MAX_TOKENS_UPPER_BOUND; a
# local (ollama) model MUST be null (uncapped). The cloud "required" message must be
# distinct from the "positive" message (the empty->null UI mapping relies on it).

def test_get_models_serves_max_tokens_config_and_per_row(client):
    body = client.get("/api/models").json()
    cfg = body["max_tokens"]
    assert cfg["default"] == config.DEFAULT_MAX_TOKENS
    assert cfg["default_reasoning"] == config.DEFAULT_MAX_TOKENS_REASONING
    assert cfg["upper_bound"] == config.MAX_TOKENS_UPPER_BOUND
    assert cfg["local_providers"] == sorted(config.LOCAL_PROVIDERS)
    rows = {m["model"]: m for m in body["models"]}
    assert rows["openai:gpt-5.4-nano"]["max_tokens"] == config.DEFAULT_MAX_TOKENS_REASONING
    assert rows["anthropic:claude-haiku-4-5"]["max_tokens"] == config.DEFAULT_MAX_TOKENS
    assert rows["ollama:qwen3.5:9b"]["max_tokens"] is None


def _put_cloud(client, **over):
    body = {"enabled": True, "supports_temperature": True, "supports_seed": False,
            "is_reasoning": False, "notes": None, "max_tokens": 2048}
    body.update(over)
    return client.put("/api/models/anthropic:claude-haiku-4-5", json=body)


def test_put_cloud_sets_max_tokens(client, seeded_db_path):
    resp = _put_cloud(client, max_tokens=2048)
    assert resp.status_code == 200
    assert resp.json()["max_tokens"] == 2048
    assert _model(seeded_db_path, "anthropic:claude-haiku-4-5")["max_tokens"] == 2048


def test_put_cloud_null_max_tokens_is_422_required(client):
    resp = _put_cloud(client, max_tokens=None)
    assert resp.status_code == 422
    assert "required" in resp.json()["detail"].lower()


def test_put_cloud_nonpositive_max_tokens_is_422_positive(client):
    resp = _put_cloud(client, max_tokens=0)
    assert resp.status_code == 422
    assert "positive" in resp.json()["detail"].lower()


def test_put_cloud_over_bound_max_tokens_is_422(client):
    resp = _put_cloud(client, max_tokens=config.MAX_TOKENS_UPPER_BOUND + 1)
    assert resp.status_code == 422


def test_put_local_nonnull_max_tokens_is_422(client):
    resp = client.put("/api/models/ollama:qwen3.5:9b", json={
        "enabled": True, "supports_temperature": True, "supports_seed": True,
        "is_reasoning": True, "notes": None, "max_tokens": 5000,
    })
    assert resp.status_code == 422


def test_put_local_null_max_tokens_stays_uncapped(client, seeded_db_path):
    resp = client.put("/api/models/ollama:qwen3.5:9b", json={
        "enabled": True, "supports_temperature": True, "supports_seed": True,
        "is_reasoning": True, "notes": None, "max_tokens": None,
    })
    assert resp.status_code == 200
    assert _model(seeded_db_path, "ollama:qwen3.5:9b")["max_tokens"] is None


def test_post_cloud_explicit_max_tokens_stored(client, seeded_db_path):
    resp = client.post("/api/models", json={
        "model": "openai:gpt-4o", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": False, "max_tokens": 1234,
    })
    assert resp.status_code == 200
    assert resp.json()["max_tokens"] == 1234
    assert _model(seeded_db_path, "openai:gpt-4o")["max_tokens"] == 1234


def test_post_cloud_omitted_max_tokens_defaults_reasoning_aware(client, seeded_db_path):
    # Non-reasoning cloud -> lean default; reasoning cloud -> generous default. And the
    # RETURNED cap must equal what actually landed in the row (anti-drift between the
    # endpoint echo and db.insert_model's _UNSET default).
    r1 = client.post("/api/models", json={
        "model": "openai:gpt-4o", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": False,
    })
    assert r1.status_code == 200
    stored1 = _model(seeded_db_path, "openai:gpt-4o")["max_tokens"]
    assert stored1 == config.DEFAULT_MAX_TOKENS
    assert r1.json()["max_tokens"] == stored1

    r2 = client.post("/api/models", json={
        "model": "openai:o5-mini", "supports_temperature": False,
        "supports_seed": True, "is_reasoning": True,
    })
    assert r2.status_code == 200
    stored2 = _model(seeded_db_path, "openai:o5-mini")["max_tokens"]
    assert stored2 == config.DEFAULT_MAX_TOKENS_REASONING
    assert r2.json()["max_tokens"] == stored2


def test_post_cloud_over_bound_is_422(client):
    resp = client.post("/api/models", json={
        "model": "openai:gpt-4o", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": False,
        "max_tokens": config.MAX_TOKENS_UPPER_BOUND + 1,
    })
    assert resp.status_code == 422


def test_post_local_omitted_max_tokens_is_null(client, seeded_db_path):
    resp = client.post("/api/models", json={
        "model": "ollama:llama4", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": True,
    })
    assert resp.status_code == 200
    assert resp.json()["max_tokens"] is None
    assert _model(seeded_db_path, "ollama:llama4")["max_tokens"] is None


def test_post_local_explicit_max_tokens_is_422(client):
    resp = client.post("/api/models", json={
        "model": "ollama:llama4", "supports_temperature": True,
        "supports_seed": True, "is_reasoning": True, "max_tokens": 5000,
    })
    assert resp.status_code == 422
