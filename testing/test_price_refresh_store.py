"""Storage seam + proposals CRUD for the price-refresh agent."""
import pytest

import db
from price_refresh import store

NOW = "2026-06-13T12:00:00-04:00"
TODAY = "2026-06-13"


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "t.db")
    db.init_db(path)
    c = db.get_connection(path)
    yield c
    c.close()


def _window(conn, model, inp, out, valid_from, valid_to, deleted=0):
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO model_prices (model, input_per_1m, output_per_1m, "
            "valid_from, valid_to, deleted, recorded_at) VALUES (?, ?, ?, ?, ?, ?, 'x')",
            (model, inp, out, valid_from, valid_to, deleted),
        )


def _windows(conn, model):
    return db.fetch_prices(conn, model, include_deleted=True)


# --- apply_price: append on a new day ---------------------------------------

def test_apply_price_appends_new_window_on_new_day(conn):
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
    new_id = store.apply_price(
        conn, model="test:m", input_per_1m=3.0, output_per_1m=4.0, now=NOW)

    rows = {r["valid_from"]: r for r in _windows(conn, "test:m")}
    assert rows[TODAY]["id"] == new_id
    assert rows[TODAY]["input_per_1m"] == 3.0 and rows[TODAY]["output_per_1m"] == 4.0
    assert rows[TODAY]["valid_to"] is None                  # new open window
    # The prior window is retained as history (the rollback target), auto-closed.
    assert rows["2026-01-01"]["valid_to"] == TODAY


# --- apply_price: idempotency (epsilon, not raw ==) -------------------------

def test_apply_price_unchanged_value_is_noop(conn):
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
    result = store.apply_price(
        conn, model="test:m", input_per_1m=1.0, output_per_1m=2.0, now=NOW)
    assert result is None
    assert len(_windows(conn, "test:m")) == 1               # no new window


def test_apply_price_epsilon_unchanged_is_noop(conn):
    # Daily extraction yields 0.050000001 vs a stored 0.05; raw == would miss it and
    # churn a useless window every day. The epsilon guard skips it.
    _window(conn, "test:m", 0.05, 0.05, "2026-01-01", None)
    result = store.apply_price(
        conn, model="test:m", input_per_1m=0.050000001,
        output_per_1m=0.050000001, now=NOW)
    assert result is None
    assert len(_windows(conn, "test:m")) == 1


# --- apply_price: same-day supersede in place -------------------------------

def test_apply_price_same_day_supersedes_in_place(conn):
    # First apply opens today's window; a second same-day apply with a CHANGED value
    # must UPDATE that row in place, not append (which would trip strictly-after).
    first_id = store.apply_price(
        conn, model="test:m", input_per_1m=3.0, output_per_1m=4.0, now=NOW)
    second_id = store.apply_price(
        conn, model="test:m", input_per_1m=3.5, output_per_1m=4.5,
        now="2026-06-13T18:00:00-04:00")

    assert second_id == first_id                            # same row, no new id
    rows = _windows(conn, "test:m")
    assert len(rows) == 1                                   # exactly one today-window
    assert rows[0]["valid_from"] == TODAY and rows[0]["valid_to"] is None
    assert rows[0]["input_per_1m"] == 3.5 and rows[0]["output_per_1m"] == 4.5


# --- apply_price: supersede targets the open non-deleted window only --------

def test_apply_price_ambiguous_today_row_fails_closed(conn):
    # A soft-deleted window dated TODAY coexisting with an OLDER open non-deleted
    # window is an ambiguous state (removed-and-re-added / editor-soft-deleted-today).
    # A CHANGED apply must fail closed — never silently update the deleted today row
    # nor append a colliding window.
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)          # open active
    _window(conn, "test:m", 9.9, 9.9, TODAY, None, deleted=1)      # deleted, today
    before = _windows(conn, "test:m")

    with pytest.raises(store.PriceRefreshError):
        store.apply_price(
            conn, model="test:m", input_per_1m=5.0, output_per_1m=6.0, now=NOW)

    after = _windows(conn, "test:m")
    assert len(after) == len(before)                         # nothing appended
    deleted = [r for r in after if r["deleted"] == 1][0]
    assert deleted["input_per_1m"] == 9.9                     # deleted row untouched


def test_apply_price_idempotency_baseline_is_open_active_not_deleted(conn):
    # In the same ambiguous state, an UNCHANGED apply (== the OPEN active value, NOT
    # the deleted today row) is a clean no-op — proving the baseline is the open
    # non-deleted window, not whatever sits dated today.
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)          # open active
    _window(conn, "test:m", 9.9, 9.9, TODAY, None, deleted=1)      # deleted, today
    result = store.apply_price(
        conn, model="test:m", input_per_1m=1.0, output_per_1m=2.0, now=NOW)
    assert result is None                                    # matched open active


# --- extractable_models -----------------------------------------------------

def test_extractable_models_excludes_local_providers(conn):
    # The seeded registry includes ollama (local, $0, no pricing page); it must be
    # excluded so check_structure never demands a model extraction can't produce.
    models = store.extractable_models(conn)
    assert "ollama:qwen3.5:9b" not in models
    assert "anthropic:claude-haiku-4-5" in models
    assert "openai:gpt-5.4-nano" in models


def test_extractable_models_excludes_soft_deleted(conn):
    with db.transaction(conn):
        db.set_model_deleted(conn, model="openai:gpt-5.4-nano", deleted=True)
    assert "openai:gpt-5.4-nano" not in store.extractable_models(conn)


# --- proposals CRUD ---------------------------------------------------------

def test_upsert_proposal_keeps_one_pending_per_model_field(conn):
    with db.transaction(conn):
        store.upsert_proposal(
            conn, model="test:m", field="input", old_value=5.0, new_value=7.0,
            pct_change=0.40, direction="up", quote="q1",
            source_url="http://a", now=NOW)
    with db.transaction(conn):
        store.upsert_proposal(
            conn, model="test:m", field="input", old_value=5.0, new_value=8.0,
            pct_change=0.60, direction="up", quote="q2",
            source_url="http://b", now="2026-06-14T12:00:00-04:00")

    rows = conn.execute(
        "SELECT * FROM price_proposals WHERE model='test:m' AND status='pending'"
    ).fetchall()
    assert len(rows) == 1                                    # upserted, not stacked
    assert rows[0]["new_value"] == 8.0                       # refreshed in place
    assert rows[0]["quote"] == "q2"
    assert rows[0]["proposed_at"] == "2026-06-14T12:00:00-04:00"


def test_resolve_proposal_sets_status_and_resolved_at(conn):
    with db.transaction(conn):
        store.upsert_proposal(
            conn, model="test:m", field="output", old_value=5.0, new_value=7.0,
            pct_change=0.40, direction="up", quote="q", source_url="u", now=NOW)
    pid = conn.execute(
        "SELECT id FROM price_proposals WHERE model='test:m'").fetchone()["id"]
    with db.transaction(conn):
        rows = store.resolve_proposal(conn, pid, "confirmed", now=NOW)
    assert rows == 1
    row = conn.execute(
        "SELECT * FROM price_proposals WHERE id=?", (pid,)).fetchone()
    assert row["status"] == "confirmed"
    assert row["resolved_at"] == NOW


# --- proposal reads ---------------------------------------------------------

def _stage(conn, *, model, field, new_value, old_value=5.0, now=NOW):
    with db.transaction(conn):
        store.upsert_proposal(
            conn, model=model, field=field, old_value=old_value,
            new_value=new_value, pct_change=0.40, direction="up", quote="q",
            source_url="http://src", now=now)
    return conn.execute(
        "SELECT id FROM price_proposals WHERE model=? AND field=? AND status='pending'",
        (model, field)).fetchone()["id"]


def test_fetch_proposal_by_id(conn):
    pid = _stage(conn, model="test:m", field="input", new_value=7.0)
    row = store.fetch_proposal(conn, pid)
    assert row is not None and row["model"] == "test:m" and row["new_value"] == 7.0
    assert store.fetch_proposal(conn, 99999) is None


def test_fetch_pending_proposals_excludes_resolved(conn):
    p1 = _stage(conn, model="test:a", field="input", new_value=7.0)
    _stage(conn, model="test:b", field="output", new_value=8.0)
    with db.transaction(conn):
        store.resolve_proposal(conn, p1, "rejected", now=NOW)
    pending = store.fetch_pending_proposals(conn)
    models = {r["model"] for r in pending}
    assert models == {"test:b"}                          # resolved p1 excluded


# --- confirm / reject -------------------------------------------------------

def test_confirm_proposal_applies_carrying_other_field_forward(conn):
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)        # active baseline
    pid = _stage(conn, model="test:m", field="output", new_value=9.0, old_value=2.0)

    assert store.confirm_proposal(conn, pid, now=NOW) is True

    active = db.active_price_window(conn, "test:m")
    assert active["valid_from"] == TODAY
    assert active["input_per_1m"] == 1.0          # carried forward (unchanged field)
    assert active["output_per_1m"] == 9.0         # the confirmed value
    assert store.fetch_proposal(conn, pid)["status"] == "confirmed"
    assert store.fetch_pending_proposals(conn) == []
    # Reversibility: the prior window is retained as history.
    rows = {r["valid_from"]: r for r in _windows(conn, "test:m")}
    assert rows["2026-01-01"]["output_per_1m"] == 2.0


def test_confirm_proposal_input_field_carries_output_forward(conn):
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
    pid = _stage(conn, model="test:m", field="input", new_value=5.0, old_value=1.0)
    assert store.confirm_proposal(conn, pid, now=NOW) is True
    active = db.active_price_window(conn, "test:m")
    assert active["input_per_1m"] == 5.0 and active["output_per_1m"] == 2.0


def test_confirm_proposal_unknown_or_resolved_returns_false(conn):
    assert store.confirm_proposal(conn, 99999, now=NOW) is False
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
    pid = _stage(conn, model="test:m", field="input", new_value=5.0)
    with db.transaction(conn):
        store.resolve_proposal(conn, pid, "rejected", now=NOW)
    assert store.confirm_proposal(conn, pid, now=NOW) is False   # already resolved


def test_reject_proposal_opens_no_window(conn):
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
    pid = _stage(conn, model="test:m", field="output", new_value=9.0)
    before = len(_windows(conn, "test:m"))

    assert store.reject_proposal(conn, pid, now=NOW) is True

    assert len(_windows(conn, "test:m")) == before          # no window opened
    assert store.fetch_proposal(conn, pid)["status"] == "rejected"
    assert store.fetch_pending_proposals(conn) == []


# --- price-change tolerance (single source) ---------------------------------

def test_value_changed_ignores_sub_tolerance_jitter():
    # A source/model emitting 0.9999996 for a published 1.000 (4e-7 < PRICE_ABS_TOL)
    # is NOT a change — far below cent-level published precision.
    assert store.value_changed(1.0, 0.9999996) is False
    assert store.value_changed(0.05, 0.050000001) is False


def test_value_changed_detects_real_sub_cent_move():
    assert store.value_changed(1.0, 0.999) is True          # a real 0.1% move
    assert store.value_changed(0.05, 0.06) is True


def test_apply_price_noops_on_sub_tolerance_jitter(conn):
    # The move detector and apply's idempotency guard share PRICE_ABS_TOL, so a jitter
    # the detector calls "unchanged" also no-ops apply — no near-identical window opens.
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
    result = store.apply_price(
        conn, model="test:m", input_per_1m=0.9999996, output_per_1m=2.0, now=NOW)
    assert result is None
    assert len(_windows(conn, "test:m")) == 1
