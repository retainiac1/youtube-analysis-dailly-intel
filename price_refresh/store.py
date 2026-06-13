"""Storage seam + proposals CRUD for the price-refresh agent.

The ONLY price_refresh module that touches the DB. apply_price is a thin seam over
the SAME db.insert_price_window the dashboard editor uses, so manual edits and agent
applies never diverge — plus a same-day SUPERSEDE path (model_prices' valid_from is
date-granular and the writer enforces strictly-increasing valid_from, so a second
whole-window write on the same day must update today's open window in place, not
append). Proposals CRUD keeps exactly one pending row per (model, field) via the
ux_pending_proposal partial index.
"""
from __future__ import annotations

import math
import sqlite3

import config
import db

# Cent-fraction tolerance for "the price did not change": daily extraction yields
# e.g. 0.050000001 vs a stored 0.05, and raw float == would churn a needless window
# every day. 1e-6 is far below any meaningful per-MTok move.
_PRICE_ABS_TOL = 1e-6

_RESOLVED_STATUSES = ("confirmed", "rejected")


class PriceRefreshError(Exception):
    """Raised when apply_price cannot safely place a window — the fail-closed guard
    for an ambiguous today-or-later state (a soft-deleted/closed row dated today, or a
    human-pre-dated future window) that is NOT the open active window. We refuse to
    guess rather than silently write the wrong window."""


def extractable_models(conn: sqlite3.Connection) -> set[str]:
    """The canonical EXTRACTABLE/PRICED model set: registry models (non-deleted) whose
    provider is not local. Local/$0 providers (config.LOCAL_PROVIDERS) have no pricing
    page, so extraction can never produce them — they must be excluded or
    check_structure would fail "missing key" on every run. This is the set the caller
    passes to validate.check_structure; NEVER the raw registry.

    Phase 0 filters local-only; a source-presence join is added in Phase 1 when the
    per-provider source map exists."""
    return {row["model"] for row in db.fetch_models(conn)
            if row["provider"] not in config.LOCAL_PROVIDERS}


def _prices_equal(window: sqlite3.Row, input_per_1m: float,
                  output_per_1m: float) -> bool:
    """True when a window's stored prices already equal the proposed values within
    tolerance (epsilon, not raw ==)."""
    return (
        math.isclose(window["input_per_1m"], input_per_1m,
                     rel_tol=0.0, abs_tol=_PRICE_ABS_TOL)
        and math.isclose(window["output_per_1m"], output_per_1m,
                         rel_tol=0.0, abs_tol=_PRICE_ABS_TOL)
    )


def apply_price(conn: sqlite3.Connection, *, model: str, input_per_1m: float,
                output_per_1m: float, now: str | None = None) -> int | None:
    """Open (or supersede) the current price window for `model` with a full
    (input, output) pair. Returns the affected window id, or None when the active
    price is already equal (a no-op). The whole read-guard-write is one atomic,
    lock-retried transaction.

    `now` is the Eastern ISO-8601 write timestamp (defaults to config.now_local_iso());
    its date (now[:10]) is the window's valid_from. Decision order:

    1. idempotency (epsilon) vs the OPEN, NON-DELETED window -> no-op (None).
    2. that open window is dated today -> SUPERSEDE it in place (no new row, no
       strictly-after collision).
    3. a today-or-later row exists that is NOT that open window -> fail closed.
    4. otherwise -> append a new window (auto-closes + retains the prior as history).
    """
    if now is None:
        now = config.now_local_iso()
    valid_from = now[:10]

    def write() -> int | None:
        with db.transaction(conn):
            active = db.active_price_window(conn, model)
            if active is not None and _prices_equal(active, input_per_1m,
                                                    output_per_1m):
                return None
            if active is not None and active["valid_from"] == valid_from:
                db.update_price_window(
                    conn, price_id=active["id"], input_per_1m=input_per_1m,
                    output_per_1m=output_per_1m, now=now)
                return active["id"]
            latest = db.latest_price_window(conn, model)
            if latest is not None and latest["valid_from"] >= valid_from:
                raise PriceRefreshError(
                    f"{model}: a price window dated {latest['valid_from']} already "
                    f"exists that is not the open active window (deleted or "
                    f"future-dated); refusing to auto-apply for {valid_from}"
                )
            return db.insert_price_window(
                conn, model=model, input_per_1m=input_per_1m,
                output_per_1m=output_per_1m, valid_from=valid_from, now=now)

    return db.run_with_db_retry(write)


def upsert_proposal(conn: sqlite3.Connection, *, model: str, field: str,
                    old_value: float, new_value: float, pct_change: float,
                    direction: str, quote: str, source_url: str,
                    now: str) -> None:
    """Stage a >=threshold move for human review, keeping exactly ONE pending row per
    (model, field): insert, or refresh the existing pending row's value/pct/direction/
    quote/source_url/proposed_at in place. The ON CONFLICT target REPEATS the
    ux_pending_proposal predicate (WHERE status='pending') — required for SQLite to
    infer a partial index; without it a second pending row would silently stack.
    Does not commit."""
    conn.execute(
        """
        INSERT INTO price_proposals (model, field, old_value, new_value, pct_change,
            direction, quote, source_url, status, proposed_at, resolved_at)
        VALUES (:model, :field, :old, :new, :pct, :dir, :quote, :url,
                'pending', :now, NULL)
        ON CONFLICT (model, field) WHERE status = 'pending'
        DO UPDATE SET old_value = excluded.old_value, new_value = excluded.new_value,
            pct_change = excluded.pct_change, direction = excluded.direction,
            quote = excluded.quote, source_url = excluded.source_url,
            proposed_at = excluded.proposed_at
        """,
        {"model": model, "field": field, "old": old_value, "new": new_value,
         "pct": pct_change, "dir": direction, "quote": quote, "url": source_url,
         "now": now},
    )


def resolve_proposal(conn: sqlite3.Connection, proposal_id: int, status: str, *,
                     now: str) -> int:
    """Mark a proposal confirmed or rejected and stamp resolved_at. Returns rowcount
    (0 = unknown id). The APPLICATION of a confirmed value (opening a window) is the
    caller's job (Phase 2); this only writes the row. Does not commit."""
    if status not in _RESOLVED_STATUSES:
        raise ValueError(
            f"status must be one of {_RESOLVED_STATUSES}, got {status!r}")
    cur = conn.execute(
        "UPDATE price_proposals SET status = :status, resolved_at = :now "
        "WHERE id = :id",
        {"status": status, "now": now, "id": proposal_id},
    )
    return cur.rowcount
