import db
import seed_models


# --- pick_grok_model (the live-list selection heuristic) -------------------

def test_pick_grok_prefers_fast_or_mini():
    # Cheap tier ('fast'/'mini') wins over the base grok.
    assert seed_models.pick_grok_model(
        ["grok-4", "grok-4-fast", "gpt-4o", "grok-3"]) == "grok-4-fast"
    assert seed_models.pick_grok_model(["grok-9", "grok-9-mini"]) == "grok-9-mini"


def test_pick_grok_falls_back_to_smallest_grok():
    # No fast/mini variant -> the lexically smallest grok.
    assert seed_models.pick_grok_model(["grok-7", "grok-3"]) == "grok-3"


def test_pick_grok_none_when_no_grok_offered():
    assert seed_models.pick_grok_model(["gpt-4o", "claude-x"]) is None


# --- correct_xai (the DB reconciliation) -----------------------------------

def test_correct_xai_is_noop_when_already_current(tmp_path):
    """When the live-confirmed string is the one already seeded, nothing changes:
    no retirement, no duplicate window."""
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            result = seed_models.correct_xai(
                conn, "xai:grok-4-fast", input_per_1m=0.20, output_per_1m=0.50,
                now="2026-06-11T10:00:00-04:00")
        assert result["retired"] == []
        rows = conn.execute(
            "SELECT model, enabled, deleted FROM models WHERE model LIKE 'xai:%'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["enabled"] == 1 and rows[0]["deleted"] == 0
        n = conn.execute(
            "SELECT count(*) c FROM model_prices WHERE model='xai:grok-4-fast'"
        ).fetchone()["c"]
        assert n == 1  # no duplicate window
    finally:
        conn.close()


def test_correct_xai_replaces_stale_string(tmp_path):
    """A different live-confirmed string: the stale grok-4-fast row is soft-deleted
    (leaves the dropdown) but its price window stays (past spend still prices), and
    the confirmed model is inserted, offered, and priced."""
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            result = seed_models.correct_xai(
                conn, "xai:grok-9-mini", input_per_1m=0.15, output_per_1m=0.45,
                now="2026-06-11T10:00:00-04:00")
        assert result["retired"] == ["xai:grok-4-fast"]

        offered = {r["model"] for r in db.fetch_dropdown_models(conn)}
        assert "xai:grok-4-fast" not in offered      # stale string hidden
        assert "xai:grok-9-mini" in offered          # confirmed string offered

        # Backward pricing preserved: retiring the MODEL leaves its price window
        # intact and non-deleted (date-independent — the seed's valid_from is
        # "today"), so fetch_spend still prices that model's history.
        win = conn.execute(
            "SELECT deleted FROM model_prices WHERE model = 'xai:grok-4-fast'"
        ).fetchone()
        assert win is not None and win["deleted"] == 0
        # The confirmed model has its open window at the given price (valid_from is
        # the explicit `now` passed above, so this date is deterministic).
        new = db.fetch_effective_price(conn, "xai:grok-9-mini", "2026-06-11")
        assert new["input_per_1m"] == 0.15 and new["output_per_1m"] == 0.45
        # Capability flags: grok honors both.
        m = db.fetch_model(conn, "xai:grok-9-mini")
        assert m["supports_temperature"] == 1 and m["supports_seed"] == 1
    finally:
        conn.close()


def test_correct_xai_restores_a_previously_retired_confirmed_string(tmp_path):
    """If the confirmed string exists but was soft-deleted, it is re-enabled rather
    than duplicated."""
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            conn.execute(
                "UPDATE models SET deleted = 1, enabled = 0 "
                "WHERE model = 'xai:grok-4-fast'"
            )
            result = seed_models.correct_xai(
                conn, "xai:grok-4-fast", input_per_1m=0.20, output_per_1m=0.50,
                now="2026-06-11T10:00:00-04:00")
        assert result["retired"] == []
        m = db.fetch_model(conn, "xai:grok-4-fast")
        assert m["enabled"] == 1 and m["deleted"] == 0  # restored, not duplicated
    finally:
        conn.close()


# --- verify_anthropic (read-only cross-check) ------------------------------

def test_verify_anthropic_reports_registry_and_logged(tmp_path):
    db_path = str(tmp_path / "t.db")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        with db.transaction(conn):
            db.log_invocation(conn, "2026-06-08", "overall",
                              "anthropic:claude-haiku-4-5", 0.5, None, None,
                              100, 30, "2026-06-08T11:00:00-04:00")
        reg, logged = seed_models.verify_anthropic(conn)
        assert reg == "anthropic:claude-haiku-4-5"
        assert logged == "anthropic:claude-haiku-4-5"  # equal => no drift
    finally:
        conn.close()
