"""Tests for the Extract page backend (dashboard/extract.py + its route).

The subprocess is ALWAYS stubbed: no test shells out to run-pipeline.sh or the
YouTube API. Pure helpers are tested synchronously; the async runner is driven with
asyncio.run() (the suite has no pytest-asyncio).
"""

import asyncio

import pytest

from dashboard import extract

# The eight (code, reason) pairs of the pipeline exit contract (config.py /
# swipefile._EXIT_REASON). Kept here as the parse target.
EXIT_CONTRACT = [
    (0, "ok"),
    (2, "network"),
    (3, "quota_exceeded"),
    (4, "auth"),
    (5, "other"),
    (6, "db_locked"),
    (7, "no_rows"),
    (8, "classify_budget_exceeded"),
]


# --- pure helpers -----------------------------------------------------------

@pytest.mark.parametrize("code,reason", EXIT_CONTRACT)
def test_parse_pipeline_result_all_reason_slugs(code, reason):
    line = f"PIPELINE_RESULT: code={code} reason={reason}"
    assert extract.parse_pipeline_result(line) == (code, reason)


def test_parse_pipeline_result_tolerates_trailing_whitespace():
    assert extract.parse_pipeline_result(
        "PIPELINE_RESULT: code=0 reason=ok\n") == (0, "ok")


@pytest.mark.parametrize("line", [
    "Searching for videos...",          # an ordinary log line
    "PIPELINE_RESULT: garbage",          # right prefix, wrong shape
    "PIPELINE_RESULT: code=5",           # missing reason
    "PIPELINE_RESULT: code=x reason=ok", # non-numeric code
    "",                                   # blank
])
def test_parse_pipeline_result_returns_none_for_non_result_lines(line):
    assert extract.parse_pipeline_result(line) is None


def test_mode_flags_maps_both_modes():
    assert extract.MODE_FLAGS == {"discover": "--discover", "dry-run": "--dry-run"}


def test_log_event_frames_text_as_sse():
    frame = extract.log_event("hello")
    assert frame == 'event: log\ndata: {"text": "hello"}\n\n'


def test_log_event_json_escapes_embedded_newline():
    # A log line with a newline must not inject a bare newline into the SSE data
    # segment (which would split the frame). JSON-encoding escapes it.
    frame = extract.log_event("a\nb")
    body = frame[: -2]  # drop the terminating blank line
    assert "\n\n" not in body          # no premature frame terminator
    assert "\\n" in frame              # newline survived as an escape


def test_result_event_frames_code_and_reason():
    assert extract.result_event(5, "timeout") == (
        'event: result\ndata: {"code": 5, "reason": "timeout"}\n\n')


# --- async runner: fakes ----------------------------------------------------

class _FakeStream:
    """A stand-in for child.stderr. Yields the canned byte lines, then either EOF
    (b"") or — when `hang` — blocks until the child's exit event is set (a wedged
    run that only ends when terminate()/kill() 'delivers a signal')."""

    def __init__(self, lines, *, hang, exit_event):
        self._lines = [b if isinstance(b, bytes) else b.encode() for b in lines]
        self._hang = hang
        self._exit = exit_event

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        if self._hang:
            await self._exit.wait()  # not set until the child is told to die
        return b""


class FakeChild:
    """A stubbed asyncio subprocess. `hang` models a wedged run: stderr stops and
    wait() blocks until terminate() (if obeys_sigterm) or kill() sets the exit event.
    `raise_on_read` makes readline raise, to exercise a crashed supervise."""

    def __init__(self, lines=(), returncode=0, *, hang=False,
                 obeys_sigterm=True, raise_on_read=False):
        self._exit = asyncio.Event()
        self.stderr = _FakeStream(lines, hang=hang, exit_event=self._exit)
        self._returncode = returncode
        self._obeys_sigterm = obeys_sigterm
        self.terminated = False
        self.killed = False
        if raise_on_read:
            async def _boom():
                raise RuntimeError("boom")
            self.stderr.readline = _boom
        if not hang:
            self._exit.set()

    async def wait(self):
        await self._exit.wait()
        return self._returncode

    def terminate(self):
        self.terminated = True
        if self._obeys_sigterm:
            self._exit.set()

    def kill(self):
        self.killed = True
        self._exit.set()

    def finish(self):
        """Simulate the child exiting on its own (the disconnect case)."""
        self._exit.set()


def _spawn_returning(child):
    async def spawn(flag):
        child.flag = flag
        return child
    return spawn


def _runner(child, **overrides):
    r = extract.ExtractRunner(spawn=_spawn_returning(child))
    # Tight timeouts so watchdog tests run fast; overridable per test.
    r.run_timeout = overrides.get("run_timeout", 5.0)
    r.grace = overrides.get("grace", 5.0)
    r.keepalive = overrides.get("keepalive", 5.0)
    return r


async def _drain(handle):
    """Collect the non-keepalive SSE frames a stream yields."""
    frames = []
    async for f in handle.stream():
        if f.startswith(": keep-alive"):
            continue
        frames.append(f)
    return frames


# --- async runner: behavior -------------------------------------------------

def test_happy_path_streams_logs_then_result():
    async def scenario():
        child = FakeChild(
            lines=["working...", "PIPELINE_RESULT: code=0 reason=ok"],
            returncode=0)
        runner = _runner(child)
        handle = await runner.start("discover")
        frames = await _drain(handle)
        # The PIPELINE_RESULT line is the machine contract, not human log output:
        # it is consumed into the terminal result, not echoed as a log line.
        assert frames == [extract.log_event("working..."),
                          extract.result_event(0, "ok")]
        assert runner._discover_active is False
        assert child.flag == "--discover"
    asyncio.run(scenario())


def test_absent_result_line_forces_other_5_not_raw_code():
    async def scenario():
        # Exit code 2 would be EXIT_NETWORK if mapped — it must NOT be.
        child = FakeChild(lines=["did stuff", "more"], returncode=2)
        runner = _runner(child)
        handle = await runner.start("discover")
        frames = await _drain(handle)
        assert frames == [extract.log_event("did stuff"),
                          extract.log_event("more"),
                          extract.result_event(5, "other")]
        assert runner._discover_active is False
    asyncio.run(scenario())


def test_watchdog_sigkills_wedged_run_and_emits_timeout():
    async def scenario():
        child = FakeChild(lines=["working..."], hang=True, obeys_sigterm=False)
        runner = _runner(child, run_timeout=0.05, grace=0.05)
        handle = await runner.start("discover")
        frames = await _drain(handle)
        assert frames == [extract.log_event("working..."),
                          extract.result_event(5, "timeout")]
        assert child.terminated is True
        assert child.killed is True            # SIGTERM ignored -> SIGKILL path
        assert runner._discover_active is False
        # claim released -> a subsequent discover may start
        runner.spawn = _spawn_returning(FakeChild(
            lines=["PIPELINE_RESULT: code=0 reason=ok"]))
        handle2 = await runner.start("discover")
        await _drain(handle2)
    asyncio.run(scenario())


def test_watchdog_child_dies_during_grace_no_sigkill():
    async def scenario():
        child = FakeChild(lines=["working..."], hang=True, obeys_sigterm=True)
        runner = _runner(child, run_timeout=0.05, grace=5.0)
        handle = await runner.start("discover")
        frames = await _drain(handle)
        assert frames[-1] == extract.result_event(5, "timeout")
        assert child.terminated is True
        assert child.killed is False           # died during grace -> no SIGKILL
    asyncio.run(scenario())


def test_discover_single_flight_raises_busy():
    async def scenario():
        runner = _runner(FakeChild())
        runner._discover_active = True          # a discover already in flight
        with pytest.raises(extract.ExtractBusy):
            await runner.start("discover")
    asyncio.run(scenario())


def test_dry_run_bypasses_the_claim():
    async def scenario():
        child = FakeChild(lines=["PIPELINE_RESULT: code=0 reason=ok"])
        runner = _runner(child)
        runner._discover_active = True          # a discover is in flight
        handle = await runner.start("dry-run")  # must NOT raise
        frames = await _drain(handle)
        assert frames == [extract.result_event(0, "ok")]
        assert child.flag == "--dry-run"
        assert runner._discover_active is True   # untouched by the dry-run
    asyncio.run(scenario())


def test_concurrent_dry_run_and_discover_both_tracked():
    async def scenario():
        disc = FakeChild(lines=["d"], hang=True)
        dry = FakeChild(lines=["p"], hang=True)

        async def spawn(flag):
            return disc if flag == "--discover" else dry

        runner = extract.ExtractRunner(spawn=spawn)
        runner.run_timeout = runner.grace = runner.keepalive = 5.0
        h_disc = await runner.start("discover")
        h_dry = await runner.start("dry-run")
        assert len(runner._tasks) == 2          # a set, not one clobbered slot
        assert runner._discover_active is True
        disc.finish()
        dry.finish()
        await h_disc.task
        await h_dry.task
        await asyncio.sleep(0)                   # flush done-callbacks
        assert runner._tasks == set()
        assert runner._discover_active is False
    asyncio.run(scenario())


def test_supervise_crash_releases_claim_and_surfaces_exception():
    async def scenario():
        child = FakeChild(raise_on_read=True)
        runner = _runner(child)
        handle = await runner.start("discover")
        with pytest.raises(RuntimeError):
            await handle.task                    # the crash propagates out of the task
        await asyncio.sleep(0)                    # let the done-callback run
        assert runner._discover_active is False   # finally released the claim
        assert handle.task not in runner._tasks   # done-callback discarded it
        assert isinstance(handle.task.exception(), RuntimeError)  # surfaced, retrieved
    asyncio.run(scenario())


def test_disconnect_keeps_claim_until_child_exits():
    async def scenario():
        child = FakeChild(lines=["working..."], hang=True)
        runner = _runner(child, run_timeout=100.0)
        handle = await runner.start("discover")
        await asyncio.sleep(0)                    # let supervise start reading
        # stream reader abandoned (client disconnected): claim + task still live
        assert runner._discover_active is True
        assert handle.task in runner._tasks
        child.finish()                            # child exits on its own
        await handle.task
        await asyncio.sleep(0)                    # flush done-callback
        assert runner._discover_active is False
        assert handle.task not in runner._tasks
    asyncio.run(scenario())


# --- route (TestClient) -----------------------------------------------------
# The route uses the module-singleton extract.runner; monkeypatch its spawn /
# _discover_active so no test shells out to the real pipeline.

def test_route_rejects_absent_mode(client):
    # mode is required (Query(...)) so a parameterless call can't default into a
    # side-effecting discover; it is a clean 422.
    assert client.get("/api/extract/stream").status_code == 422


def test_route_rejects_unknown_mode(client):
    assert client.get("/api/extract/stream?mode=bogus").status_code == 422


def test_route_returns_409_when_discover_in_flight(client, monkeypatch):
    monkeypatch.setattr(extract.runner, "_discover_active", True)
    assert client.get("/api/extract/stream?mode=discover").status_code == 409


def test_route_streams_run_events(client, monkeypatch):
    async def fake_spawn(flag):
        return FakeChild(lines=["working...", "PIPELINE_RESULT: code=0 reason=ok"])

    monkeypatch.setattr(extract.runner, "spawn", fake_spawn)
    r = client.get("/api/extract/stream?mode=dry-run")
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert extract.log_event("working...") in r.text
    assert extract.result_event(0, "ok") in r.text
