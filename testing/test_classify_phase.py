"""Tests for the swipefile classification phase: caching, failover, the mass-failure
guard, pruning, and preflight. The LLM is faked via generate_fns / monkeypatched
_make_classify_generate, so nothing touches the network or a real DB."""
import json
from types import SimpleNamespace

import pytest

import classify
import config
import llm
import swipefile

PRIMARY = "google:gemini-2.5-flash-lite"
FALLBACK = "anthropic:claude-haiku-4-5"


@pytest.fixture(autouse=True)
def _no_state_writes(monkeypatch):
    # save_state writes STATE_FILE; the phase calls it. Silence it in unit tests.
    monkeypatch.setattr(swipefile, "save_state", lambda state: None)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # The retry layer sleeps between attempts; record calls instead of waiting so
    # the backoff/retryDelay logic is testable without real delays.
    calls = []
    monkeypatch.setattr(swipefile.time, "sleep", lambda s: calls.append(s))
    return calls


def _err(status=None, retry_after=None, msg="provider down"):
    return llm.LLMError(msg, status_code=status, retry_after_seconds=retry_after)


class RecordingGen:
    """A generate_fn that records every call and delegates to a behavior(prompt, i)
    callback returning a GenerateResult-shaped object or raising."""

    def __init__(self, behavior):
        self.calls = []
        self._behavior = behavior

    def __call__(self, prompt):
        i = len(self.calls)
        self.calls.append(prompt)
        return self._behavior(prompt, i)


def _result(english=True, kids_targeted=False, kids_subject=False,
            input_tokens=100, output_tokens=15):
    text = json.dumps({"english": english, "kids_targeted": kids_targeted,
                       "kids_subject": kids_subject, "reason": "r"})
    return SimpleNamespace(text=text, input_tokens=input_tokens,
                           output_tokens=output_tokens)


def _video(vid, title):
    return {"id": vid, "snippet": {"title": title, "description": "",
                                   "channelTitle": "c"}}


def _router_gen(by_title, default=None):
    """A generate_fn returning a verdict chosen by which title appears in the prompt.
    `default` answers anything unrouted (e.g. the preflight probe); without one an
    unrouted prompt is a test bug."""
    def gen(prompt):
        for needle, result in by_title.items():
            if needle in prompt:
                return result
        if default is not None:
            return default
        raise AssertionError("no routed verdict for prompt")
    return gen


def _raising_gen(exc=None):
    def gen(prompt):
        raise (exc or llm.LLMError("provider down"))
    return gen


def test_survivors_pruned_and_tallied():
    seen = {"a": _video("a", "KEEPME"), "b": _video("b", "SPANISH"),
            "c": _video("c", "KIDSVID")}
    query_map = {"a": ["q"], "b": ["q"], "c": ["q"]}
    gen = _router_gen({
        "KEEPME": _result(english=True),
        "SPANISH": _result(english=False),
        "KIDSVID": _result(kids_subject=True),
    })
    state = {}
    tally, budget_exhausted = swipefile._classify_survivors(
        state, seen, query_map, PRIMARY, [PRIMARY], {PRIMARY: gen})
    assert tally == {"kept": 1, "not_english": 1, "kids": 1, "classify_error": 0}
    assert budget_exhausted is False
    assert set(seen) == {"a"}                 # the two drops were pruned
    assert set(query_map) == {"a"}


def test_failover_to_fallback_when_primary_errors():
    seen = {"a": _video("a", "KEEPME")}
    query_map = {"a": ["q"]}
    gens = {PRIMARY: _raising_gen(), FALLBACK: _router_gen({"KEEPME": _result()})}
    state = {}
    tally, _ = swipefile._classify_survivors(
        state, seen, query_map, PRIMARY, [PRIMARY, FALLBACK], gens)
    assert tally["kept"] == 1
    # The cached verdict records the model that actually produced it (the fallback).
    assert state["classification"]["a"]["model"] == FALLBACK


def test_both_providers_fail_one_video_is_dropped():
    seen = {"a": _video("a", "KEEPME")}
    query_map = {"a": ["q"]}
    gens = {PRIMARY: _raising_gen(), FALLBACK: _raising_gen()}
    state = {}
    tally, _ = swipefile._classify_survivors(
        state, seen, query_map, PRIMARY, [PRIMARY, FALLBACK], gens)
    assert tally["classify_error"] == 1
    assert seen == {}                          # fail-closed: dropped


def test_mass_failure_guard_aborts():
    seen = {f"v{i}": _video(f"v{i}", f"T{i}") for i in range(6)}
    query_map = {k: ["q"] for k in seen}
    gens = {PRIMARY: _raising_gen()}
    with pytest.raises(classify.ClassificationUnavailable):
        swipefile._classify_survivors(state := {}, seen, query_map, PRIMARY,
                                      [PRIMARY], gens)


def test_cache_hit_skips_call():
    seen = {"a": _video("a", "KEEPME")}
    query_map = {"a": ["q"]}
    state = {"classification": {"a": {
        "english": True, "kids_targeted": False, "kids_subject": False,
        "reason": "cached", "model": PRIMARY,
        "prompt_version": classify.CLASSIFY_PROMPT_VERSION, "keep": True}}}
    tally, _ = swipefile._classify_survivors(
        state, seen, query_map, PRIMARY, [PRIMARY], {PRIMARY: _raising_gen()})
    assert tally["kept"] == 1                  # used cache; the gen would have raised


def test_cache_invalidated_on_prompt_version_change():
    seen = {"a": _video("a", "KEEPME")}
    query_map = {"a": ["q"]}
    state = {"classification": {"a": {
        "english": False, "kids_targeted": False, "kids_subject": False,
        "reason": "stale", "model": PRIMARY,
        "prompt_version": classify.CLASSIFY_PROMPT_VERSION - 1, "keep": False}}}
    gen = _router_gen({"KEEPME": _result(english=True)})
    tally, _ = swipefile._classify_survivors(
        state, seen, query_map, PRIMARY, [PRIMARY], {PRIMARY: gen})
    # Re-classified under the current prompt -> now kept, stamped current.
    assert tally["kept"] == 1
    assert state["classification"]["a"]["prompt_version"] == classify.CLASSIFY_PROMPT_VERSION


def test_phase_skips_when_no_survivors(monkeypatch):
    # An empty discover must not make preflight LLM calls or be able to abort.
    def boom(*a, **k):
        raise AssertionError("must not build a generate_fn for an empty survivor set")

    monkeypatch.setattr(swipefile, "_make_classify_generate", boom)
    tally, budget_exhausted = swipefile._run_classification_phase(
        None, {}, {}, {}, "2026-06-18")
    assert tally == {"kept": 0, "not_english": 0, "kids": 0, "classify_error": 0}
    assert budget_exhausted is False


def test_phase_raises_when_both_providers_dead(monkeypatch):
    monkeypatch.setattr(classify, "active_classification_model", lambda conn: PRIMARY)
    monkeypatch.setattr(classify, "active_classification_fallback_model",
                        lambda conn: FALLBACK)
    monkeypatch.setattr(llm, "api_key_present", lambda provider: True)
    monkeypatch.setattr(swipefile, "_make_classify_generate",
                        lambda conn, model, run_date: _raising_gen())
    seen = {"a": _video("a", "KEEPME")}
    with pytest.raises(classify.ClassificationUnavailable):
        swipefile._run_classification_phase(None, {}, seen, {"a": ["q"]}, "2026-06-18")


def test_phase_uses_fallback_when_primary_preflight_dead(monkeypatch):
    monkeypatch.setattr(classify, "active_classification_model", lambda conn: PRIMARY)
    monkeypatch.setattr(classify, "active_classification_fallback_model",
                        lambda conn: FALLBACK)
    monkeypatch.setattr(llm, "api_key_present", lambda provider: True)
    gens = {PRIMARY: _raising_gen(),
            FALLBACK: _router_gen({"KEEPME": _result()}, default=_result())}
    monkeypatch.setattr(swipefile, "_make_classify_generate",
                        lambda conn, model, run_date: gens[model])
    seen = {"a": _video("a", "KEEPME")}
    query_map = {"a": ["q"]}
    tally, budget_exhausted = swipefile._run_classification_phase(
        None, {}, seen, query_map, "2026-06-18")
    assert tally["kept"] == 1
    assert budget_exhausted is False
    assert set(seen) == {"a"}


# --- retry: attempt counts (max_retries=1 => 2 attempts per model per video) -

def test_primary_retried_then_succeeds_no_failover():
    # 429 on the first primary call, success on the retry: classified on the PRIMARY,
    # the fallback is never touched.
    keep = _result()
    primary = RecordingGen(
        lambda p, i: keep if i >= 1 else (_ for _ in ()).throw(_err(429)))
    fallback = RecordingGen(lambda p, i: keep)
    seen = {"a": _video("a", "KEEPME")}
    tally, _ = swipefile._classify_survivors(
        {}, seen, {"a": ["q"]}, PRIMARY, [PRIMARY, FALLBACK],
        {PRIMARY: primary, FALLBACK: fallback})
    assert tally["kept"] == 1
    assert len(primary.calls) == 2           # initial + 1 retry
    assert len(fallback.calls) == 0          # never failed over


def test_primary_two_attempts_then_failover():
    # 429 on BOTH primary attempts -> exactly 2 primary calls, then failover to Haiku.
    primary = RecordingGen(lambda p, i: (_ for _ in ()).throw(_err(429)))
    fallback = RecordingGen(lambda p, i: _result())
    seen = {"a": _video("a", "KEEPME")}
    state = {}
    tally, _ = swipefile._classify_survivors(
        state, seen, {"a": ["q"]}, PRIMARY, [PRIMARY, FALLBACK],
        {PRIMARY: primary, FALLBACK: fallback})
    assert len(primary.calls) == 2
    assert len(fallback.calls) == 1
    assert tally["kept"] == 1
    assert state["classification"]["a"]["model"] == FALLBACK


def test_fallback_two_attempts_then_unclassified():
    # Primary down, fallback errors through BOTH its attempts -> 2 fallback calls,
    # then the video is counted unclassified (classify_error), fail-closed.
    primary = RecordingGen(lambda p, i: (_ for _ in ()).throw(_err(429)))
    fallback = RecordingGen(lambda p, i: (_ for _ in ()).throw(_err(500)))
    seen = {"a": _video("a", "KEEPME")}
    tally, _ = swipefile._classify_survivors(
        {}, seen, {"a": ["q"]}, PRIMARY, [PRIMARY, FALLBACK],
        {PRIMARY: primary, FALLBACK: fallback})
    assert len(fallback.calls) == 2
    assert tally["classify_error"] == 1
    assert seen == {}


# --- sticky failover: ONLY a 429 exhaust condemns the rest of the run --------

def test_sticky_on_429_skips_primary_for_later_videos():
    primary = RecordingGen(lambda p, i: (_ for _ in ()).throw(_err(429)))
    fallback = RecordingGen(lambda p, i: _result())
    seen = {"a": _video("a", "AAA"), "b": _video("b", "BBB")}
    query_map = {"a": ["q"], "b": ["q"]}
    tally, _ = swipefile._classify_survivors(
        {}, seen, query_map, PRIMARY, [PRIMARY, FALLBACK],
        {PRIMARY: primary, FALLBACK: fallback})
    # Video a exhausts the primary (2 calls) and sets sticky; video b skips the
    # primary entirely -> primary called only for a, fallback for both.
    assert len(primary.calls) == 2
    assert len(fallback.calls) == 2
    assert tally["kept"] == 2


def test_503_is_not_sticky_primary_retried_for_each_video():
    primary = RecordingGen(lambda p, i: (_ for _ in ()).throw(_err(503)))
    fallback = RecordingGen(lambda p, i: _result())
    seen = {"a": _video("a", "AAA"), "b": _video("b", "BBB")}
    query_map = {"a": ["q"], "b": ["q"]}
    swipefile._classify_survivors(
        {}, seen, query_map, PRIMARY, [PRIMARY, FALLBACK],
        {PRIMARY: primary, FALLBACK: fallback})
    # 503 is transient: each video retries the primary (2 calls each) before failing
    # over -> 4 primary calls total, primary stays live across videos.
    assert len(primary.calls) == 4
    assert len(fallback.calls) == 2


def test_classification_error_is_not_sticky():
    # A parse glitch (ClassificationError) on the primary is per-video, not sticky.
    primary = RecordingGen(
        lambda p, i: (_ for _ in ()).throw(classify.ClassificationError("bad json")))
    fallback = RecordingGen(lambda p, i: _result())
    seen = {"a": _video("a", "AAA"), "b": _video("b", "BBB")}
    query_map = {"a": ["q"], "b": ["q"]}
    swipefile._classify_survivors(
        {}, seen, query_map, PRIMARY, [PRIMARY, FALLBACK],
        {PRIMARY: primary, FALLBACK: fallback})
    assert len(primary.calls) == 4           # retried per video, never condemned
    assert len(fallback.calls) == 2


# --- backoff: honor 429 retryDelay; 503 uses exponential backoff ------------

def test_429_honors_retry_after(_no_sleep):
    # retry_after_seconds from the error governs the wait (capped at max_backoff).
    primary = RecordingGen(
        lambda p, i: _result() if i >= 1 else
        (_ for _ in ()).throw(_err(429, retry_after=2.0)))
    seen = {"a": _video("a", "KEEPME")}
    swipefile._classify_survivors(
        {}, seen, {"a": ["q"]}, PRIMARY, [PRIMARY], {PRIMARY: primary})
    assert _no_sleep == [2.0]                 # honored the retryDelay, once


def test_429_retry_after_capped_at_max_backoff(_no_sleep, monkeypatch):
    monkeypatch.setattr(config, "CLASSIFICATION_RETRY",
                        {**config.CLASSIFICATION_RETRY, "max_backoff_seconds": 5.0})
    primary = RecordingGen(
        lambda p, i: _result() if i >= 1 else
        (_ for _ in ()).throw(_err(429, retry_after=99.0)))
    seen = {"a": _video("a", "KEEPME")}
    swipefile._classify_survivors(
        {}, seen, {"a": ["q"]}, PRIMARY, [PRIMARY], {PRIMARY: primary})
    assert _no_sleep == [5.0]                 # capped


def test_503_uses_exponential_backoff(_no_sleep):
    # No retryDelay on a 503; the wait is base * 2**attempt (attempt 0 -> base).
    primary = RecordingGen(
        lambda p, i: _result() if i >= 1 else (_ for _ in ()).throw(_err(503)))
    seen = {"a": _video("a", "KEEPME")}
    swipefile._classify_survivors(
        {}, seen, {"a": ["q"]}, PRIMARY, [PRIMARY], {PRIMARY: primary})
    base = config.CLASSIFICATION_RETRY["base_backoff_seconds"]
    assert _no_sleep == [base]


# --- per-run fallback spend breaker -----------------------------------------

def test_fallback_budget_breaker_aborts_without_wasted_call(monkeypatch):
    N = 2
    monkeypatch.setattr(config, "CLASSIFICATION_RETRY",
                        {**config.CLASSIFICATION_RETRY, "max_retries": 0,
                         "max_fallback_videos": N})
    primary = RecordingGen(lambda p, i: (_ for _ in ()).throw(_err(429)))
    fallback = RecordingGen(lambda p, i: _result())
    seen = {f"v{i}": _video(f"v{i}", f"T{i}") for i in range(N + 1)}
    query_map = {k: ["q"] for k in seen}
    state = {}
    tally, budget_exhausted = swipefile._classify_survivors(
        state, seen, query_map, PRIMARY, [PRIMARY, FALLBACK],
        {PRIMARY: primary, FALLBACK: fallback})
    assert budget_exhausted is True
    # Exactly N fallback classifications happened; the (N+1)th tripped with NO call.
    assert len(fallback.calls) == N
    # The N classified-and-kept survivors are persisted; the tripping one is pruned.
    assert len(seen) == N
    assert all(state["classification"][v]["model"] == FALLBACK for v in seen)


def test_fallback_budget_allows_exactly_the_cap(monkeypatch):
    # N fallback videos succeed (cap is inclusive); no trip when count == cap.
    N = 3
    monkeypatch.setattr(config, "CLASSIFICATION_RETRY",
                        {**config.CLASSIFICATION_RETRY, "max_retries": 0,
                         "max_fallback_videos": N})
    primary = RecordingGen(lambda p, i: (_ for _ in ()).throw(_err(429)))
    fallback = RecordingGen(lambda p, i: _result())
    seen = {f"v{i}": _video(f"v{i}", f"T{i}") for i in range(N)}
    query_map = {k: ["q"] for k in seen}
    tally, budget_exhausted = swipefile._classify_survivors(
        {}, seen, query_map, PRIMARY, [PRIMARY, FALLBACK],
        {PRIMARY: primary, FALLBACK: fallback})
    assert budget_exhausted is False
    assert tally["kept"] == N
    assert len(fallback.calls) == N


# --- phase-level forced-429 failover (below the breaker cap) -----------------

def test_phase_all_failover_to_haiku_below_cap(monkeypatch):
    monkeypatch.setattr(classify, "active_classification_model", lambda conn: PRIMARY)
    monkeypatch.setattr(classify, "active_classification_fallback_model",
                        lambda conn: FALLBACK)
    monkeypatch.setattr(llm, "api_key_present", lambda provider: True)
    # Primary preflight + every classify call 429s; the fallback always succeeds.
    gens = {PRIMARY: RecordingGen(lambda p, i: (_ for _ in ()).throw(_err(429))),
            FALLBACK: RecordingGen(lambda p, i: _result())}
    monkeypatch.setattr(swipefile, "_make_classify_generate",
                        lambda conn, model, run_date: gens[model])
    seen = {f"v{i}": _video(f"v{i}", f"T{i}") for i in range(20)}  # << 150 cap
    query_map = {k: ["q"] for k in seen}
    tally, budget_exhausted = swipefile._run_classification_phase(
        None, {}, seen, query_map, "2026-06-18")
    assert budget_exhausted is False
    assert tally["classify_error"] == 0
    assert tally["kept"] == 20
    assert set(seen) == set(query_map) == {f"v{i}" for i in range(20)}
