import sqlite3

import config
import db
import swipefile

NOW1 = "2026-06-07T10:00:00-04:00"
NOW2 = "2026-06-07T12:00:00-04:00"
NOW3 = "2026-06-08T09:00:00-04:00"


def fresh_db(tmp_path) -> sqlite3.Connection:
    db_path = str(tmp_path / "test.db")
    db.init_db(db_path)
    return db.get_connection(db_path)


# --- quota_ledger -----------------------------------------------------------

def test_get_units_used_absent_date_is_zero(tmp_path):
    conn = fresh_db(tmp_path)
    try:
        assert db.get_units_used(conn, "2026-06-07") == 0
    finally:
        conn.close()


def test_add_quota_units_fresh_then_increment(tmp_path):
    conn = fresh_db(tmp_path)
    try:
        db.add_quota_units(conn, "2026-06-07", 100, NOW1)   # fresh row
        assert db.get_units_used(conn, "2026-06-07") == 100
        db.add_quota_units(conn, "2026-06-07", 6, NOW2)      # increment same day
        assert db.get_units_used(conn, "2026-06-07") == 106
    finally:
        conn.close()


def test_add_quota_units_starts_fresh_row_on_new_pacific_date(tmp_path):
    conn = fresh_db(tmp_path)
    try:
        db.add_quota_units(conn, "2026-06-07", 106, NOW1)
        db.add_quota_units(conn, "2026-06-08", 1, NOW3)      # new date -> new row
        assert db.get_units_used(conn, "2026-06-08") == 1
        assert db.get_units_used(conn, "2026-06-07") == 106  # prior day untouched
    finally:
        conn.close()


def test_add_quota_units_commits_its_own_transaction(tmp_path):
    """The eager flush must survive a later rollback on the same connection: it
    commits independently, so a subsequent rolled-back write cannot undo it."""
    conn = fresh_db(tmp_path)
    try:
        db.add_quota_units(conn, "2026-06-07", 100, NOW1)
        # Start an unrelated write and roll it back.
        conn.execute("INSERT INTO videos (video_id) VALUES ('x')")
        conn.rollback()
        assert db.get_units_used(conn, "2026-06-07") == 100  # flush survived
    finally:
        conn.close()


# --- QuotaBudget ------------------------------------------------------------

def test_budget_can_afford_and_charge():
    b = swipefile.QuotaBudget(baseline=9000, cap=9500)
    assert b.can_afford(500) is True
    assert b.can_afford(501) is False        # baseline+cost would exceed cap
    b.charge(400)
    assert b.run_units == 400
    assert b.remaining() == 100              # 9500 - 9000 - 400
    assert b.can_afford(101) is False


def test_budget_unflushed_tracks_remainder():
    b = swipefile.QuotaBudget(baseline=0, cap=9500)
    b.charge(100)
    b.flushed_units += 100                    # eager flush of a search.list
    b.charge(6)                               # cheap calls, not yet flushed
    assert b.unflushed() == 6


# --- api_call_with_retry per-call guard -------------------------------------

def test_api_call_guard_refuses_before_call():
    b = swipefile.QuotaBudget(baseline=9499, cap=9500)
    called = []
    result = swipefile.api_call_with_retry(lambda: called.append(1) or "x", b, cost=100)
    assert result is None                      # refused
    assert b.guard_stopped is True
    assert called == []                        # underlying call never made
    assert b.run_units == 0                    # nothing charged


def test_api_call_charges_on_success():
    b = swipefile.QuotaBudget(baseline=0, cap=9500)
    result = swipefile.api_call_with_retry(lambda: "ok", b, cost=100)
    assert result == "ok"
    assert b.run_units == 100
    assert b.guard_stopped is False


# --- estimate parity with dry-run -------------------------------------------

def test_estimate_total_is_sum_of_breakdown():
    est = swipefile.estimate_discover_units(eligible_count=100)
    assert est["total"] == (est["search"] + est["videos"] + est["channels"]
                            + est["comments"] + est["sweep"])


def test_estimate_sweep_term_ceils_by_batch_size():
    """The sweep term is ceil(eligible / CHANNEL_BATCH_SIZE) videos.list units."""
    b = swipefile.CHANNEL_BATCH_SIZE
    cost = swipefile.VIDEOS_QUOTA_COST
    assert swipefile.estimate_discover_units(0)["sweep"] == 0
    assert swipefile.estimate_discover_units(1)["sweep"] == 1 * cost
    assert swipefile.estimate_discover_units(b)["sweep"] == 1 * cost
    assert swipefile.estimate_discover_units(b + 1)["sweep"] == 2 * cost


def test_dry_run_prints_the_shared_estimate(capsys):
    """--dry-run and the pre-flight must agree: the printed total is exactly
    estimate_discover_units(eligible)['total'], and the sweep term is shown."""
    est = swipefile.estimate_discover_units(eligible_count=120)
    swipefile._print_dry_run(units_today=0, cap=9500, eligible_count=120)
    err = capsys.readouterr().err
    assert f"~{est['total']} units" in err
    assert f"= {est['sweep']}" in err          # the sweep breakdown line is printed


# --- _pick_status precedence ------------------------------------------------

def _budget(guard=False):
    b = swipefile.QuotaBudget(0, 100)
    b.guard_stopped = guard
    return b


def test_pick_status_precedence():
    # signature: (budget, quota_aborted, persist_partial, downgraded, classify_budget)
    assert swipefile._pick_status(_budget(guard=True), True, True, True, True) == "quota_guard_stop"
    assert swipefile._pick_status(_budget(), True, True, True, True) == "quota_exceeded"
    # The classify breaker ranks below the two YouTube quota stops but above partial.
    assert swipefile._pick_status(_budget(), False, True, True, True) == "classify_budget_stop"
    assert swipefile._pick_status(_budget(), False, True, True, False) == "partial"
    assert swipefile._pick_status(_budget(), False, False, True, False) == "discover_downgraded_to_refresh"
    assert swipefile._pick_status(_budget(), False, False, False, False) == "success"


def test_pick_status_youtube_quota_wins_over_classify_breaker():
    # Both a reactive YouTube quota stop AND the classify breaker fired: the
    # not-retryable-today daily blocker wins the label (so the run maps to EXIT_QUOTA).
    assert swipefile._pick_status(
        _budget(), True, False, False, True) == "quota_exceeded"


# --- _discover_done_today ---------------------------------------------------

def _add_run(conn, mode, started_at, status):
    conn.execute(
        "INSERT INTO run_log (mode, started_at, finished_at, quota_used, "
        "videos_seen, status) VALUES (?, ?, ?, 0, 0, ?)",
        (mode, started_at, started_at, status),
    )
    conn.commit()


def test_discover_done_today_success_or_partial(tmp_path):
    conn = fresh_db(tmp_path)
    try:
        today = config.pacific_date("2026-06-07T14:00:00-04:00")
        assert swipefile._discover_done_today(conn, today) is False
        _add_run(conn, "discover", "2026-06-07T14:00:00-04:00", "partial")
        assert swipefile._discover_done_today(conn, today) is True
    finally:
        conn.close()


def test_discover_done_today_ignores_failed_and_other_dates(tmp_path):
    conn = fresh_db(tmp_path)
    try:
        today = config.pacific_date("2026-06-07T14:00:00-04:00")
        _add_run(conn, "discover", "2026-06-07T14:00:00-04:00", "failed")
        _add_run(conn, "discover", "2026-06-06T14:00:00-04:00", "success")  # prior day
        _add_run(conn, "refresh", "2026-06-07T15:00:00-04:00", "success")   # wrong mode
        assert swipefile._discover_done_today(conn, today) is False
    finally:
        conn.close()


def test_discover_done_today_uses_pacific_boundary(tmp_path):
    """A discover started at 02:30 Eastern belongs to the PREVIOUS Pacific day,
    so it does not count as done for the current Pacific date."""
    conn = fresh_db(tmp_path)
    try:
        _add_run(conn, "discover", "2026-06-07T02:30:00-04:00", "success")
        # That run's Pacific date is 2026-06-06.
        assert swipefile._discover_done_today(conn, "2026-06-06") is True
        assert swipefile._discover_done_today(conn, "2026-06-07") is False
    finally:
        conn.close()
