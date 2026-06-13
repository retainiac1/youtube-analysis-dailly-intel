"""Daily orchestration: extraction -> classification -> apply/stage, + the
invocation-logging generate_text closure."""
import sys

import pytest

import db
import llm
from price_refresh import daily, extract

RUN_DATE = "2026-06-13"
NOW = "2026-06-13T12:00:00-04:00"
EXTRACTION_MODEL = "anthropic:claude-haiku-4-5"


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "t.db")
    db.init_db(path)
    c = db.get_connection(path)
    yield c
    c.close()


# --- make_generate_text (dedicated unit test, not via a fake) ---------------

def test_make_generate_text_returns_text_and_logs_invocation(conn, monkeypatch):
    calls = {}

    def fake_generate(model, prompt, **kwargs):
        calls["model"] = model
        calls["kwargs"] = kwargs
        return llm.GenerateResult(text='{"ok": true}', input_tokens=123,
                                  output_tokens=45, seed_applied=7)

    monkeypatch.setattr(llm, "generate", fake_generate)
    gen = daily.make_generate_text(conn, EXTRACTION_MODEL, RUN_DATE)
    out = gen("the prompt")

    assert out == '{"ok": true}'
    assert calls["model"] == EXTRACTION_MODEL
    assert calls["kwargs"]["temperature"] == 0.0

    rows = conn.execute(
        "SELECT * FROM llm_invocations WHERE scope = 'price_extraction'"
    ).fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["run_date"] == RUN_DATE and r["model"] == EXTRACTION_MODEL
    assert r["input_tokens"] == 123 and r["output_tokens"] == 45
    assert r["seed"] == 7
    assert r["duration_ms"] is not None


def test_make_generate_text_converts_llm_error_to_extraction_error(conn, monkeypatch):
    def boom(model, prompt, **kwargs):
        raise llm.LLMError("anthropic: rate limit")

    monkeypatch.setattr(llm, "generate", boom)
    gen = daily.make_generate_text(conn, EXTRACTION_MODEL, RUN_DATE)
    with pytest.raises(extract.ExtractionError, match="rate limit"):
        gen("p")
    # A failed call logs no invocation row.
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM llm_invocations").fetchone()["c"] == 0


def test_make_generate_text_unknown_model_raises(conn):
    with pytest.raises(Exception):
        daily.make_generate_text(conn, "nope:nope", RUN_DATE)


# --- run_daily end-to-end (fetch + generate_text injected) ------------------
import json     # noqa: E402
import re       # noqa: E402

from price_refresh import store    # noqa: E402

TODAY = "2026-06-13"
SRC = {"test": ["https://a", "https://b"]}
MODELS = {"test:m"}


def _window(conn, model, inp, out, valid_from, valid_to, deleted=0):
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO model_prices (model, input_per_1m, output_per_1m, "
            "valid_from, valid_to, deleted, recorded_at) VALUES (?, ?, ?, ?, ?, ?, 'x')",
            (model, inp, out, valid_from, valid_to, deleted),
        )


def _windows(conn, model):
    return db.fetch_prices(conn, model, include_deleted=True)


def _fetch(url_to_page):
    def fetch(url):
        page = url_to_page.get(url)
        if page is None:
            raise extract.ExtractionError(f"fetch failed for {url}")
        return page
    return fetch


def _generate(url_to_payload):
    """generate_text fake: read the Source URL from the prompt, return its canned JSON
    payload (model -> {input, output, quote})."""
    def gen(prompt):
        url = re.search(r"Source URL: (\S+)", prompt).group(1)
        return json.dumps(url_to_payload[url])
    return gen


def _run(conn, *, payloads, now=NOW, pages=None, models=MODELS, source_map=SRC):
    pages = pages if pages is not None else {u: "PAGE" for u in payloads}
    return daily.run_daily(
        conn, now=now, fetch=_fetch(pages),
        generate_text=_generate(payloads), source_map=source_map, models=models)


def _both(payload):
    return {"https://a": payload, "https://b": payload}


def _p(inp, out, quote="q"):
    return {"test:m": {"input": inp, "output": out, "quote": quote}}


def test_auto_applies_small_agreed_change(conn):
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
    s = _run(conn, payloads=_both(_p(1.04, 2.0)))    # input +4% AUTO, output unchanged
    active = db.active_price_window(conn, "test:m")
    assert active["valid_from"] == TODAY
    assert active["input_per_1m"] == 1.04 and active["output_per_1m"] == 2.0
    assert s.applied == 1 and s.staged == 0
    assert store.fetch_pending_proposals(conn) == []


def test_large_change_stages_proposal_no_window(conn):
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
    s = _run(conn, payloads=_both(_p(1.5, 2.0)))     # input +50% REVIEW
    assert db.active_price_window(conn, "test:m")["valid_from"] == "2026-01-01"
    pending = store.fetch_pending_proposals(conn)
    assert len(pending) == 1
    assert pending[0]["field"] == "input" and pending[0]["new_value"] == 1.5
    assert s.staged == 1 and s.applied == 0


def test_unit_error_rejected(conn):
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
    s = _run(conn, payloads=_both(_p(1000.0, 2.0)))  # input 1000x > hi=200 -> REJECT
    assert db.active_price_window(conn, "test:m")["valid_from"] == "2026-01-01"
    assert store.fetch_pending_proposals(conn) == []
    assert s.rejected == 1 and s.applied == 0


def test_no_change_day_opens_nothing(conn):
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
    s = _run(conn, payloads=_both(_p(1.0, 2.0)))
    assert db.active_price_window(conn, "test:m")["valid_from"] == "2026-01-01"
    assert store.fetch_pending_proposals(conn) == []
    assert s.applied == 0 and s.staged == 0


def test_sub_tolerance_jitter_is_noop(conn):
    # Extraction emits 0.9999996 for a stored 1.0 (a 4e-7 jitter < PRICE_ABS_TOL): the
    # move detector and apply guard agree (shared constant) -> no window, no proposal.
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
    s = _run(conn, payloads=_both(_p(0.9999996, 2.0)))
    assert db.active_price_window(conn, "test:m")["valid_from"] == "2026-01-01"
    assert store.fetch_pending_proposals(conn) == []
    assert s.applied == 0 and s.staged == 0


def test_idempotent_rerun_same_day(conn):
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
    _run(conn, payloads=_both(_p(1.04, 2.0)), now=NOW)
    _run(conn, payloads=_both(_p(1.04, 2.0)), now=NOW)
    today_windows = [w for w in _windows(conn, "test:m") if w["valid_from"] == TODAY]
    assert len(today_windows) == 1                   # no duplicate window


def test_persisting_move_keeps_one_proposal(conn):
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
    _run(conn, payloads=_both(_p(1.5, 2.0)), now=NOW)
    _run(conn, payloads=_both(_p(1.5, 2.0)), now="2026-06-14T12:00:00-04:00")
    assert len(store.fetch_pending_proposals(conn)) == 1     # upserted, not stacked


def test_lagging_source_passthrough_is_review_not_reject(conn):
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
    # source A moved (1.5), source B still matches the active window (1.0).
    s = _run(conn, payloads={"https://a": _p(1.5, 2.0, "moved-a"),
                             "https://b": _p(1.0, 2.0, "lagging-b")})
    pending = store.fetch_pending_proposals(conn)
    assert s.rejected == 0 and len(pending) == 1
    assert pending[0]["field"] == "input" and pending[0]["new_value"] == 1.5
    assert pending[0]["quote"] == "moved-a"          # the moved source's citation


def test_single_source_changed_stages_review(conn):
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
    # B fails to fetch; A shows a small (would-be-AUTO) 4% move -> uncross-checked REVIEW.
    s = _run(conn, payloads={"https://a": _p(1.04, 2.0)},
             pages={"https://a": "PAGE", "https://b": None})
    assert db.active_price_window(conn, "test:m")["valid_from"] == "2026-01-01"
    assert s.staged == 1 and s.applied == 0
    assert store.fetch_pending_proposals(conn)[0]["field"] == "input"


def test_single_source_unchanged_is_noop(conn):
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
    s = _run(conn, payloads={"https://a": _p(1.0, 2.0)},
             pages={"https://a": "PAGE", "https://b": None})
    assert db.active_price_window(conn, "test:m")["valid_from"] == "2026-01-01"
    assert s.applied == 0 and s.staged == 0


def test_per_field_independence_auto_input_review_output(conn):
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
    # input +4% AUTO, output +50% REVIEW.
    s = _run(conn, payloads=_both(_p(1.04, 3.0)))
    active = db.active_price_window(conn, "test:m")
    assert active["valid_from"] == TODAY
    assert active["input_per_1m"] == 1.04            # auto-applied
    assert active["output_per_1m"] == 2.0            # carried forward (output under review)
    pending = store.fetch_pending_proposals(conn)
    assert len(pending) == 1 and pending[0]["field"] == "output"
    assert pending[0]["new_value"] == 3.0
    assert s.applied == 1 and s.staged == 1


def test_inversion_demotes_moved_auto_field(conn):
    # Near-inverted baseline; a small AUTO input move pushes input above output.
    _window(conn, "test:m", 1.9, 2.0, "2026-01-01", None)
    s = _run(conn, payloads=_both(_p(2.05, 2.0)))    # input +7.9% AUTO, now > output
    assert db.active_price_window(conn, "test:m")["valid_from"] == "2026-01-01"  # no window
    pending = store.fetch_pending_proposals(conn)
    assert len(pending) == 1 and pending[0]["field"] == "input"   # the moved field demoted
    assert s.applied == 0 and s.staged == 1


def test_reversibility_prior_window_retained(conn):
    _window(conn, "test:m", 1.0, 2.0, "2026-01-01", None)
    _run(conn, payloads=_both(_p(1.04, 2.0)))
    rows = {r["valid_from"]: r for r in _windows(conn, "test:m")}
    assert rows["2026-01-01"]["input_per_1m"] == 1.0    # prior value still recoverable
    assert rows["2026-01-01"]["valid_to"] == TODAY      # auto-closed, retained


# --- main() CLI wiring ------------------------------------------------------

def test_main_inits_db_and_calls_run_daily(tmp_path, monkeypatch):
    called = {}

    def fake_run_daily(conn, **kwargs):
        called["conn"] = conn
        return daily.RunSummary()

    monkeypatch.setattr(daily, "run_daily", fake_run_daily)
    dbp = str(tmp_path / "m.db")
    monkeypatch.setattr(sys, "argv", ["daily", "--db-path", dbp])

    daily.main()

    assert called.get("conn") is not None
    c = db.get_connection(dbp)
    try:
        assert c.execute(
            "SELECT name FROM sqlite_master WHERE name='price_proposals'"
        ).fetchone() is not None                         # init_db ran
    finally:
        c.close()
