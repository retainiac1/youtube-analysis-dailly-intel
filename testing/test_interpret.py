import pytest

import db
import interpret
import llm


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


def _make_fake_generate(rec, *, text="One. Two. Three.", input_tokens=120,
                        output_tokens=40, seed_applied="echo"):
    """A stand-in for llm.generate that records its call and returns a real
    GenerateResult. seed_applied="echo" returns the seed it was passed (a
    seed-honoring provider); pass an explicit value (e.g. None) to model a
    provider that drops the seed."""
    def fake(model, prompt, *, temperature, seed):
        rec.append({"model": model, "prompt": prompt,
                    "temperature": temperature, "seed": seed})
        applied = seed if seed_applied == "echo" else seed_applied
        return llm.GenerateResult(text=text, input_tokens=input_tokens,
                                  output_tokens=output_tokens,
                                  seed_applied=applied)
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
                        _make_fake_generate(rec, text="A summary.",
                                            input_tokens=200, output_tokens=30,
                                            seed_applied=42))
    _seed_ranking(conn, "2026-06-09", "overall", 1, "v1", 3.0)
    conn.commit()

    result = interpret.synthesize_lane(
        conn, "2026-06-09", "overall", "openai:gpt-5.4-nano",
        temperature=0.7, seed=42,
    )
    assert result["skipped"] is False
    assert result["text"] == "A summary."
    # The measured run time is returned and is a non-negative int (timed around the
    # mocked generate(), so it is tiny but present).
    assert isinstance(result["duration_ms"], int) and result["duration_ms"] >= 0
    assert len(rec) == 1                              # generate called once
    assert rec[0]["model"] == "openai:gpt-5.4-nano"

    interp = conn.execute(
        "SELECT scope, text, model FROM interpretations"
    ).fetchall()
    assert len(interp) == 1
    assert interp[0]["model"] == "openai:gpt-5.4-nano"
    assert interp[0]["text"] == "A summary."

    inv = conn.execute(
        "SELECT model, temperature, seed, filter, input_tokens, "
        "output_tokens, duration_ms FROM llm_invocations"
    ).fetchall()
    assert len(inv) == 1
    assert inv[0]["model"] == "openai:gpt-5.4-nano"
    assert inv[0]["temperature"] == 0.7
    assert inv[0]["seed"] == 42
    assert inv[0]["filter"] is None
    assert inv[0]["input_tokens"] == 200
    assert inv[0]["output_tokens"] == 30
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
