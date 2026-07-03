import json

import pytest

import config
import db
import interpret
import llm


def _valid_contract_json(based_on=None):
    """A schema-conformant interpretation response: every section a string, a grounded
    recommendation. Built from interpret.INTERP_SECTION_KEYS so it tracks the contract."""
    keys = list(interpret.INTERP_SECTION_KEYS)
    return json.dumps({
        "sections": {k: f"insight {k}" for k in keys},
        "recommendation": {
            "suggestion": "make more X",
            "based_on": list(based_on) if based_on is not None else keys[:2],
        },
    })


# --- fixtures --------------------------------------------------------------
# `db_path` initializes a fresh temp DB; `conn` layers an open connection on top
# and closes it after the test. CLI tests take `db_path` because they use separate
# connections around main() (mirroring the real separate-process boundary).

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    connection = db.get_connection(db_path)
    yield connection
    connection.close()


# --- test fixtures: seed lanes ---------------------------------------------
# A ranking row always exists; the videos row is optional so a LEFT-JOIN NULL
# (missing video) can be exercised. fetch_lane LEFT JOINs videos on video_id.


def _seed_ranking(conn, run_date, bucket, rank, video_id, metric_value,
                  *, with_video=True, title="Cool Short", channel_title="Chan",
                  view_count=100000, ratio=2.5, like_count=None, comment_count=None,
                  views_per_day=None, duration_seconds=None, published_at=None,
                  matched_queries=None, top_comments=None):
    conn.execute(
        "INSERT INTO rankings (run_date, bucket, rank, video_id, metric_value, "
        "captured_at) VALUES (?, ?, ?, ?, ?, ?)",
        (run_date, bucket, rank, video_id, metric_value,
         "2026-06-08T10:00:00-04:00"),
    )
    if with_video:
        conn.execute(
            "INSERT INTO videos (video_id, title, channel_title, link, "
            "thumbnail_url, view_count, views_to_subs_ratio, like_count, "
            "comment_count, views_per_day, duration_seconds, published_at, "
            "matched_queries, top_comments) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (video_id, title, channel_title, "http://y/" + video_id,
             "http://t/" + video_id, view_count, ratio, like_count, comment_count,
             views_per_day, duration_seconds, published_at, matched_queries,
             top_comments),
        )


def _make_fake_generate(rec, *, text=None, input_tokens=120,
                        output_tokens=40, seed_applied="echo"):
    """A stand-in for llm.generate that records its call and returns a real
    GenerateResult. `text` defaults to a valid contract JSON (so the validator passes
    and a conformant object is stored); pass a custom/broken string to exercise the
    degrade path. seed_applied="echo" returns the seed it was passed (a seed-honoring
    provider); pass an explicit value (e.g. None) to model a provider that drops the
    seed. `think`/`is_reasoning` are recorded; the applied think mirrors the adapter
    contract (None when think did not apply, else int)."""
    if text is None:
        text = _valid_contract_json()
    def fake(model, prompt, *, temperature, seed, supports_temperature=None,
             supports_seed=None, think=None, is_reasoning=False, max_tokens=None):
        rec.append({"model": model, "prompt": prompt,
                    "temperature": temperature, "seed": seed,
                    "supports_temperature": supports_temperature,
                    "supports_seed": supports_seed,
                    "think": think, "is_reasoning": is_reasoning,
                    "max_tokens": max_tokens})
        applied = seed if seed_applied == "echo" else seed_applied
        think_applied = None if think is None else int(think)
        thinking = "[reasoning]" if think else None
        return llm.GenerateResult(text=text, input_tokens=input_tokens,
                                  output_tokens=output_tokens,
                                  seed_applied=applied, thinking=thinking,
                                  think_applied=think_applied)
    return fake


# --- build_prompt ----------------------------------------------------------

def test_build_prompt_states_scope_and_exact_count(conn):
    _seed_ranking(conn, "2026-06-09", "health", 1, "v1", 3.1,
                  title="Sleep Hacks", channel_title="DrSleep")
    conn.commit()
    rows = db.fetch_lane(conn, "2026-06-09", "health")

    prompt = interpret.build_prompt("2026-06-09", "health", rows)
    assert "health" in prompt
    assert "1" in prompt              # the literal row count
    assert "Sleep Hacks" in prompt
    assert "DrSleep" in prompt


def test_build_prompt_count_matches_row_count(conn):
    for i in range(3):
        _seed_ranking(conn, "2026-06-09", "overall", i + 1, f"v{i}", 3.0 - i)
    conn.commit()
    rows = db.fetch_lane(conn, "2026-06-09", "overall")

    prompt = interpret.build_prompt("2026-06-09", "overall", rows)
    assert "3" in prompt
    assert len(rows) == 3


def test_build_prompt_tolerates_null_video_fields(conn):
    # A ranking whose video row is missing: title/channel/views are NULL via the
    # LEFT JOIN. The prompt must NOT contain the literal string "None".
    _seed_ranking(conn, "2026-06-09", "habit", 1, "ghost", 4.0, with_video=False)
    conn.commit()
    rows = db.fetch_lane(conn, "2026-06-09", "habit")

    assert rows[0]["title"] is None          # precondition: NULL field present
    # Default fields now include the new nullable columns too; none may leak "None".
    prompt = interpret.build_prompt("2026-06-09", "habit", rows)
    assert "None" not in prompt


# --- prompt field selection -------------------------------------------------

def test_normalize_fields_defaults_and_sanitizes():
    # None / empty / all-unknown -> the default set.
    assert interpret.normalize_fields(None) == interpret.DEFAULT_PROMPT_FIELDS
    assert interpret.normalize_fields([]) == interpret.DEFAULT_PROMPT_FIELDS
    assert interpret.normalize_fields(["bogus"]) == interpret.DEFAULT_PROMPT_FIELDS
    # Unknown dropped, deduped, returned in canonical order (not input order).
    assert interpret.normalize_fields(
        ["like_count", "view_count", "like_count", "nope"]
    ) == ["view_count", "like_count"]


def test_build_prompt_renders_only_selected_fields(conn):
    _seed_ranking(conn, "2026-06-09", "health", 1, "v1", 3.1, title="Sleep",
                  channel_title="DrSleep", view_count=50000, ratio=4.0,
                  like_count=820, comment_count=0, views_per_day=15774,
                  duration_seconds=17, published_at="2026-06-08T01:00:00-04:00",
                  matched_queries="high protein|gym")
    conn.commit()
    rows = db.fetch_lane(conn, "2026-06-09", "health")

    prompt = interpret.build_prompt("2026-06-09", "health", rows,
                                    ["like_count", "comment_count", "views_per_day",
                                     "duration_seconds", "published_at",
                                     "matched_queries"])
    assert "820 likes" in prompt
    assert "0 comments" in prompt            # 0 shown, not hidden
    assert "15774/day" in prompt             # views_per_day cast to int
    assert "17s" in prompt
    assert "published 2026-06-08" in prompt  # date only
    assert 'matched "high protein, gym"' in prompt
    # NOT selected -> absent.
    assert "views/subs" not in prompt
    assert "by DrSleep" not in prompt
    assert "50000 views" not in prompt


def test_build_prompt_top_comments_trimmed(conn):
    long_c = "x" * 200
    tc = f"@a: first ({1})|@b: {long_c} ({2})|@c: third|@d: fourth"
    _seed_ranking(conn, "2026-06-09", "health", 1, "v1", 3.1, top_comments=tc)
    conn.commit()
    rows = db.fetch_lane(conn, "2026-06-09", "health")

    prompt = interpret.build_prompt("2026-06-09", "health", rows, ["top_comments"])
    assert "comments:" in prompt
    assert "@a: first" in prompt and "@c: third" in prompt
    assert "@d: fourth" not in prompt        # capped at 3
    assert ("x" * 200) not in prompt         # long comment trimmed
    assert "…" in prompt


# --- synthesize_lane -------------------------------------------------------

def test_synthesize_lane_writes_interpretation_and_logs(conn, monkeypatch):
    rec = []
    monkeypatch.setattr(interpret, "generate",
                        _make_fake_generate(rec, input_tokens=200,
                                            output_tokens=30, seed_applied=42))
    _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0)
    conn.commit()

    result = interpret.synthesize_lane(
        conn, "2026-06-09", "overall", "openai:gpt-5.4-nano",
        temperature=0.7, seed=42,
    )
    assert result["skipped"] is False
    # The stored text is the contract JSON, code-stamped, all 12 sections as strings.
    parsed = json.loads(result["text"])
    assert parsed["contract_version"] == interpret.INTERP_CONTRACT_VERSION == 1
    assert parsed["context_mode"] == "aggregated"
    assert set(parsed["sections"]) == set(interpret.INTERP_SECTION_KEYS)
    assert all(isinstance(v, str) for v in parsed["sections"].values())
    assert set(parsed["recommendation"]["based_on"]).issubset(
        interpret.INTERP_SECTION_KEYS)
    assert result["partial"] is False
    # The measured run time is returned and is a non-negative int (timed around the
    # mocked generate(), so it is tiny but present).
    assert isinstance(result["duration_ms"], int) and result["duration_ms"] >= 0
    assert len(rec) == 1                              # generate called once
    assert rec[0]["model"] == "openai:gpt-5.4-nano"

    interp = conn.execute(
        "SELECT scope, text, model, temperature, seed FROM interpretations"
    ).fetchall()
    assert len(interp) == 1
    assert interp[0]["model"] == "openai:gpt-5.4-nano"
    stored = json.loads(interp[0]["text"])
    assert stored["contract_version"] == 1
    assert set(stored["sections"]) == set(interpret.INTERP_SECTION_KEYS)
    # The applied parameters are persisted on the interpretation row too.
    assert interp[0]["temperature"] == 0.7
    assert interp[0]["seed"] == 42

    inv = conn.execute(
        "SELECT model, temperature, seed, filter, input_tokens, "
        "output_tokens, duration_ms, context_mode FROM llm_invocations"
    ).fetchall()
    assert len(inv) == 1
    assert inv[0]["model"] == "openai:gpt-5.4-nano"
    assert inv[0]["temperature"] == 0.7
    assert inv[0]["seed"] == 42
    assert inv[0]["filter"] is None
    assert inv[0]["input_tokens"] == 200
    assert inv[0]["output_tokens"] == 30
    # The context mode is recorded on the invocation.
    assert inv[0]["context_mode"] == "aggregated"
    # The logged duration matches what the result reported.
    assert inv[0]["duration_ms"] == result["duration_ms"]


def test_synthesize_lane_logs_seed_applied_not_user_seed(conn, monkeypatch):
    # The user typed seed=42 but the provider dropped it (seed_applied=None).
    # The log must record the truth (NULL), not what the user typed.
    monkeypatch.setattr(interpret, "generate",
                        _make_fake_generate([], seed_applied=None))
    _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0)
    conn.commit()
    interpret.synthesize_lane(conn, "2026-06-09", "overall",
                              "anthropic:claude-haiku-4-5",
                              temperature=0.5, seed=42)
    seed = conn.execute("SELECT seed FROM llm_invocations").fetchone()["seed"]
    assert seed is None


def test_synthesize_lane_persists_applied_think(conn, monkeypatch):
    # The applied think (result.think_applied) is what gets persisted to
    # interpretations.think, threaded through the same way as seed. think on -> 1,
    # off -> 0 (NOT NULL), and a model where think did not apply -> NULL. The fresh
    # result also carries the transient thinking content for immediate display.
    monkeypatch.setattr(interpret, "generate", _make_fake_generate([]))
    _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0)
    conn.commit()

    def run(think):
        interpret.synthesize_lane(conn, "2026-06-09", "overall",
                                  "ollama:qwen3.5:9b", temperature=0.5, seed=7,
                                  think=think)
        return conn.execute(
            "SELECT think FROM interpretations WHERE scope = 'overall'"
        ).fetchone()["think"]

    assert run(True) == 1
    assert run(False) == 0          # honored-off persists 0, never NULL
    assert run(None) is None        # not applicable persists NULL

    # The fresh result dict exposes the (transient) thinking content for display.
    result = interpret.synthesize_lane(conn, "2026-06-09", "overall",
                                       "ollama:qwen3.5:9b", temperature=0.5,
                                       seed=7, think=True)
    assert result["thinking"] == "[reasoning]"
    assert result["think_applied"] == 1


def test_synthesize_lane_passes_is_reasoning_to_generate(conn, monkeypatch):
    # synthesize_lane reads is_reasoning from the model row and threads it into
    # generate() (the gate for think), keeping llm.py DB-free.
    rec = []
    monkeypatch.setattr(interpret, "generate", _make_fake_generate(rec))
    _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0)
    conn.commit()
    interpret.synthesize_lane(conn, "2026-06-09", "overall", "ollama:qwen3.5:9b",
                              temperature=0.5, seed=7, think=True)
    # ollama:qwen3.5:9b is seeded is_reasoning=1.
    assert rec[0]["is_reasoning"] is True
    assert rec[0]["think"] is True


def test_synthesize_lane_threads_max_tokens_from_row(conn, monkeypatch):
    # synthesize_lane reads max_tokens from the model row and threads it into
    # generate() like the other capability columns. gpt-5.4-nano is a reasoning cloud
    # model seeded with the generous default.
    rec = []
    monkeypatch.setattr(interpret, "generate", _make_fake_generate(rec))
    _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0)
    conn.commit()
    interpret.synthesize_lane(conn, "2026-06-09", "overall", "openai:gpt-5.4-nano",
                              temperature=0.5, seed=7)
    assert rec[0]["max_tokens"] == config.DEFAULT_MAX_TOKENS_REASONING


def test_synthesize_lane_threads_null_max_tokens_for_local(conn, monkeypatch):
    # A local (ollama) row is uncapped: NULL max_tokens threads through as None so the
    # adapter omits the cap.
    rec = []
    monkeypatch.setattr(interpret, "generate", _make_fake_generate(rec))
    _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0)
    conn.commit()
    interpret.synthesize_lane(conn, "2026-06-09", "overall", "ollama:qwen3.5:9b",
                              temperature=0.5, seed=7, think=True)
    assert rec[0]["max_tokens"] is None


def test_synthesize_lane_empty_skips_llm_and_writes_nothing(conn, monkeypatch):
    rec = []
    monkeypatch.setattr(interpret, "generate", _make_fake_generate(rec))
    result = interpret.synthesize_lane(
        conn, "2026-06-09", "health", "openai:gpt-5.4-nano",
        temperature=1.0, seed=None,
    )
    assert result["skipped"] is True
    assert rec == []                                  # LLM NOT called
    assert conn.execute(
        "SELECT COUNT(*) c FROM interpretations").fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM llm_invocations").fetchone()["c"] == 0


def test_synthesize_lane_rerun_overwrites_interp_appends_log(conn, monkeypatch):
    monkeypatch.setattr(interpret, "generate", _make_fake_generate([]))
    _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0)
    conn.commit()
    interpret.synthesize_lane(conn, "2026-06-09", "overall",
                              "openai:gpt-5.4-nano", temperature=1.0, seed=None)
    interpret.synthesize_lane(conn, "2026-06-09", "overall",
                              "openai:gpt-5.4-nano", temperature=1.0, seed=None)
    assert conn.execute(
        "SELECT COUNT(*) c FROM interpretations").fetchone()["c"] == 1
    assert conn.execute(
        "SELECT COUNT(*) c FROM llm_invocations").fetchone()["c"] == 2


def test_synthesize_lane_rolls_back_log_on_upsert_failure(conn, monkeypatch):
    # Pinned write order: log_invocation runs first, then upsert_interpretation,
    # both inside ONE transaction. If the upsert raises, the rollback must also
    # remove the already-inserted invocation row (no orphan log).
    monkeypatch.setattr(interpret, "generate", _make_fake_generate([]))

    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(db, "upsert_interpretation", boom)

    _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0)
    conn.commit()
    with pytest.raises(RuntimeError):
        interpret.synthesize_lane(conn, "2026-06-09", "overall",
                                  "openai:gpt-5.4-nano", temperature=1.0,
                                  seed=None)
    assert conn.execute(
        "SELECT COUNT(*) c FROM llm_invocations").fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM interpretations").fetchone()["c"] == 0


# --- synthesize_run --------------------------------------------------------

def test_synthesize_run_mixed_written_and_skipped(conn, monkeypatch):
    rec = []
    monkeypatch.setattr(interpret, "generate", _make_fake_generate(rec))
    # overall + health populated; habit left empty.
    _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0)
    _seed_ranking(conn, "2026-06-09", "health", 1, "v2", 2.0)
    conn.commit()

    results = interpret.synthesize_run(conn, "2026-06-09", "openai:gpt-5.4-nano",
                                       temperature=0.7, seed=None)
    by_scope = {r["scope"]: r for r in results}
    assert by_scope["overall"]["skipped"] is False
    assert by_scope["health"]["skipped"] is False
    assert by_scope["habit"]["skipped"] is True
    # Two real lanes -> two generate calls, both with the run's model+params.
    assert len(rec) == 2
    assert all(c["model"] == "openai:gpt-5.4-nano" for c in rec)
    assert all(c["temperature"] == 0.7 for c in rec)


# --- CLI -------------------------------------------------------------------

def test_cli_defaults_to_latest_run_date(db_path, monkeypatch):
    monkeypatch.setattr(interpret, "DB_PATH", db_path)
    monkeypatch.setattr(interpret, "generate", _make_fake_generate([]))
    conn = db.get_connection(db_path)
    try:
        # Two run_dates; latest is 2026-06-09.
        _seed_ranking(conn, "2026-06-07", "overall", 1, "old", 1.0)
        _seed_ranking(conn, "2026-06-09", "overall", 1, "new", 3.0)
        conn.commit()
    finally:
        conn.close()

    interpret.main(["--model", "openai:gpt-5.4-nano", "--scope", "overall",
                    "--seed", "42"])

    conn = db.get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT run_date, seed FROM llm_invocations").fetchall()
        assert len(rows) == 1
        assert rows[0]["run_date"] == "2026-06-09"        # latest, not derived
        assert rows[0]["seed"] == 42                      # seed reached the log
    finally:
        conn.close()


def test_cli_scope_limits_to_one_lane(db_path, monkeypatch):
    monkeypatch.setattr(interpret, "DB_PATH", db_path)
    monkeypatch.setattr(interpret, "generate", _make_fake_generate([]))
    conn = db.get_connection(db_path)
    try:
        _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0)
        _seed_ranking(conn, "2026-06-09", "health", 1, "v2", 2.0)
        conn.commit()
    finally:
        conn.close()

    interpret.main(["--model", "openai:gpt-5.4-nano", "--scope", "health"])

    conn = db.get_connection(db_path)
    try:
        scopes = [r["scope"] for r in conn.execute(
            "SELECT scope FROM interpretations").fetchall()]
        assert scopes == ["health"]
    finally:
        conn.close()


# --- validate_interpretation -----------------------------------------------

def test_validate_interpretation_accepts_valid_object():
    v = interpret.validate_interpretation(_valid_contract_json())
    assert v["ok"] is True
    assert set(v["sections"]) == set(interpret.INTERP_SECTION_KEYS)
    assert v["missing"] == []
    assert v["errors"] == []
    keys = list(interpret.INTERP_SECTION_KEYS)
    assert v["recommendation"]["based_on"] == keys[:2]


def test_validate_interpretation_fills_and_flags_missing_sections():
    keys = list(interpret.INTERP_SECTION_KEYS)
    obj = {
        "sections": {k: f"insight {k}" for k in keys[3:]},   # first 3 missing
        "recommendation": {"suggestion": "do X", "based_on": [keys[5]]},
    }
    v = interpret.validate_interpretation(obj)
    assert v["ok"] is False
    for k in keys[:3]:
        assert v["sections"][k] == interpret.INTERP_NO_DATA
        assert k in v["missing"]
    assert v["sections"][keys[3]] == f"insight {keys[3]}"    # present preserved
    assert set(v["sections"]) == set(keys)                   # all 12 present


def test_validate_interpretation_degrades_on_malformed_json():
    v = interpret.validate_interpretation("this is not json {")
    assert v["ok"] is False
    assert v["missing"] == list(interpret.INTERP_SECTION_KEYS)
    assert all(val == interpret.INTERP_NO_DATA for val in v["sections"].values())
    assert v["recommendation"]["based_on"] == []
    assert v["errors"]                                       # a reason recorded


def test_validate_interpretation_drops_sentinel_and_unknown_based_on():
    keys = list(interpret.INTERP_SECTION_KEYS)
    sections = {k: f"insight {k}" for k in keys}
    sentinel_key = keys[0]
    sections[sentinel_key] = interpret.INTERP_NO_DATA        # an empty section
    obj = {
        "sections": sections,
        "recommendation": {
            "suggestion": "do X",
            "based_on": [sentinel_key, "not_a_real_key", keys[1]],
        },
    }
    v = interpret.validate_interpretation(obj)
    # sentinel-valued and unknown keys dropped; the real non-sentinel one kept.
    assert v["recommendation"]["based_on"] == [keys[1]]


def test_validate_interpretation_fully_empty_window_ok():
    obj = {
        "sections": {k: interpret.INTERP_NO_DATA
                     for k in interpret.INTERP_SECTION_KEYS},
        "recommendation": {"suggestion": interpret.INTERP_NO_DATA, "based_on": []},
    }
    v = interpret.validate_interpretation(obj)
    # Empty based_on is valid when every section is the sentinel: no grounding error.
    assert v["errors"] == []
    assert v["ok"] is True
    assert v["recommendation"]["based_on"] == []


# --- context modes ----------------------------------------------------------

def test_synthesize_lane_records_aggregated_context_mode(conn, monkeypatch):
    rec = []
    monkeypatch.setattr(interpret, "generate", _make_fake_generate(rec))
    _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0)
    conn.commit()
    result = interpret.synthesize_lane(
        conn, "2026-06-09", "overall", "openai:gpt-5.4-nano",
        temperature=1.0, seed=None, context_mode="aggregated")
    assert result["context_mode"] == "aggregated"
    assert "computed aggregates" in rec[0]["prompt"]        # aggregated builder used
    mode = conn.execute(
        "SELECT context_mode FROM llm_invocations").fetchone()["context_mode"]
    assert mode == "aggregated"


def test_synthesize_lane_raw_mode_dumps_rows_and_records_mode(conn, monkeypatch):
    rec = []
    monkeypatch.setattr(interpret, "generate", _make_fake_generate(rec))
    seen_cols = []
    orig_rows = db._dashboard_video_rows

    def spy(conn_, bucket, start, end, select_cols, extra_join=""):
        seen_cols.append(select_cols)
        return orig_rows(conn_, bucket, start, end, select_cols, extra_join)
    monkeypatch.setattr(db, "_dashboard_video_rows", spy)

    _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0, title="Raw One")
    conn.commit()
    result = interpret.synthesize_lane(
        conn, "2026-06-09", "overall", "openai:gpt-5.4-nano",
        temperature=1.0, seed=None, context_mode="raw")
    assert result["context_mode"] == "raw"
    # The raw path pulled the broad per-video column set (beyond the population
    # check that uses only v.video_id).
    assert interpret._RAW_SELECT_COLS in seen_cols
    mode = conn.execute(
        "SELECT context_mode FROM llm_invocations").fetchone()["context_mode"]
    assert mode == "raw"
    assert json.loads(result["text"])["context_mode"] == "raw"


def test_synthesize_lane_rejects_bad_context_mode(conn, monkeypatch):
    monkeypatch.setattr(interpret, "generate", _make_fake_generate([]))
    _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0)
    conn.commit()
    with pytest.raises(llm.LLMError):
        interpret.synthesize_lane(
            conn, "2026-06-09", "overall", "openai:gpt-5.4-nano",
            temperature=1.0, seed=None, context_mode="bogus")


# --- raw-mode estimate + spend cap ------------------------------------------

def test_estimate_raw_interpretation_priced_breakdown(conn):
    # A priced paid model over a seeded lane: coherent breakdown, under the default cap.
    # The estimate prices at TODAY's date, which init_db's seeded window (valid_from=today)
    # covers, so no custom price window is needed.
    _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0)
    conn.commit()
    est = interpret.estimate_raw_interpretation(
        conn, "overall", "2026-06-09", "2026-06-09", "openai:gpt-5.4-nano")
    assert est["row_count"] == 1
    assert est["est_input_tokens"] > 0
    assert est["est_output_tokens"] == config.INTERP_EST_OUTPUT_TOKENS
    assert est["est_cost_usd"] is not None and est["est_cost_usd"] > 0
    assert est["cap_usd"] == config.INTERP_RAW_COST_CAP_USD
    assert est["over_cap"] is False and est["refused"] is False


def test_estimate_raw_interpretation_local_is_free(conn):
    # A local (ollama) model is free: est_cost_usd 0.0, never refused, whatever the cap.
    _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0)
    conn.commit()
    est = interpret.estimate_raw_interpretation(
        conn, "overall", "2026-06-09", "2026-06-09", "ollama:qwen3.5:9b")
    assert est["est_cost_usd"] == 0.0
    assert est["refused"] is False and est["reason"] is None


def test_estimate_raw_interpretation_paid_unpriced_refused(conn):
    # A PAID model whose current price window is gone cannot be bounded: fail closed.
    _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0)
    conn.execute("DELETE FROM model_prices WHERE model = ?", ("openai:gpt-5.4-nano",))
    conn.commit()
    est = interpret.estimate_raw_interpretation(
        conn, "overall", "2026-06-09", "2026-06-09", "openai:gpt-5.4-nano")
    assert est["est_cost_usd"] is None
    assert est["refused"] is True and est["reason"] == "unpriced"


def test_synthesize_lane_raw_over_cap_refuses_without_spending(conn, monkeypatch):
    rec = []
    monkeypatch.setattr(interpret, "generate", _make_fake_generate(rec))
    # A cap tiny enough that any priced run exceeds it.
    monkeypatch.setattr(config, "INTERP_RAW_COST_CAP_USD", 1e-12)
    _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0)
    conn.commit()
    result = interpret.synthesize_lane(
        conn, "2026-06-09", "overall", "openai:gpt-5.4-nano",
        temperature=1.0, seed=None, context_mode="raw")
    assert result["refused"] is True
    assert result["estimate"]["reason"] == interpret.INTERP_REFUSE_OVER_CAP
    assert rec == []                                  # generate NOT called: no spend
    assert conn.execute(
        "SELECT COUNT(*) c FROM interpretations").fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM llm_invocations").fetchone()["c"] == 0


def test_synthesize_lane_aggregated_ignores_cap(conn, monkeypatch):
    # Aggregated mode never estimates or caps: a tiny cap does not block it.
    rec = []
    monkeypatch.setattr(interpret, "generate", _make_fake_generate(rec))
    monkeypatch.setattr(config, "INTERP_RAW_COST_CAP_USD", 1e-12)
    _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0)
    conn.commit()
    result = interpret.synthesize_lane(
        conn, "2026-06-09", "overall", "openai:gpt-5.4-nano",
        temperature=1.0, seed=None, context_mode="aggregated")
    assert result.get("refused") is None and result["skipped"] is False
    assert len(rec) == 1                              # generate called: run proceeds
    assert conn.execute(
        "SELECT COUNT(*) c FROM interpretations").fetchone()["c"] == 1
