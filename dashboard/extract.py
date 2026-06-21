"""Backend for the dashboard Extract page: run/stream a discover (or dry-run) of
the pipeline and relay its progress over SSE.

Spawns scripts/run-pipeline.sh (never swipefile.py directly, so the WAL-safe pre-run
backup always runs) and reads its stderr line by line. Each line becomes an SSE `log`
event; the single `PIPELINE_RESULT: code=N reason=slug` line the pipeline prints on exit
becomes the terminal `result` event.

The run is owned by a background task that outlives the HTTP request, so a client
disconnect tears down only the stream, never the child or the single-flight claim. See
ExtractRunner for the concurrency contract.

Pure helpers (parse + SSE framing + the mode allow-list) are stdlib-only and unit-tested
without a server.
"""

import asyncio
import json
import re
import sys
from pathlib import Path

import config

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PIPELINE = _REPO_ROOT / "scripts" / "run-pipeline.sh"

# The modes the page exposes, mapped to the run-pipeline.sh flag each spawns. A
# structural allow-list (like app.VALID_LANES), NOT a tunable: an unknown mode is a 422.
# --refresh (the scheduled path) is deliberately not offered here.
MODE_FLAGS = {"discover": "--discover", "dry-run": "--dry-run"}

# The exact line swipefile._finalize prints to stderr on exit. Anchored so a stray
# mention in other log output never parses as the contract line.
_RESULT_RE = re.compile(r"^PIPELINE_RESULT: code=(\d+) reason=(\S+)$")


def parse_pipeline_result(line):
    """Parse a `PIPELINE_RESULT: code=N reason=slug` line into (code, reason).

    Returns None for anything else (ordinary log output, a malformed/partial line),
    so the caller treats an absent contract line as a missing result rather than a
    misparse. The raw line may carry a trailing newline; it is stripped first."""
    m = _RESULT_RE.match(line.strip())
    if m is None:
        return None
    return int(m.group(1)), m.group(2)


def _sse(event, payload):
    """Frame one SSE event. The payload is JSON-encoded so embedded newlines/quotes
    cannot split the frame (a bare newline would terminate the data segment early)."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def log_event(text):
    """SSE frame for one streamed stderr line."""
    return _sse("log", {"text": text})


def result_event(code, reason):
    """SSE frame for the terminal run result (the exit code + reason slug)."""
    return _sse("result", {"code": code, "reason": reason})


# --- the run/stream machinery ----------------------------------------------

# Queue sentinel marking the end of a run's event stream.
_DONE = object()


class ExtractBusy(Exception):
    """A discover run is already in flight. The route maps this to HTTP 409. Kept a
    plain domain exception (not HTTPException) so the runner unit-tests without
    FastAPI."""


async def _default_spawn(flag):
    """Spawn run-pipeline.sh (NEVER swipefile.py directly: the wrapper runs the
    WAL-safe pre-run backup). stdout is discarded — all human output and the
    PIPELINE_RESULT contract line go to stderr."""
    return await asyncio.create_subprocess_exec(
        str(_PIPELINE), flag,
        cwd=str(_REPO_ROOT),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )


class _Handle:
    """What a started run hands back: the SSE stream to attach to the response, and
    a reference to the owning background task (for tests / introspection). Consuming
    the stream is OPTIONAL — the run lives in the task, not the stream, so a client
    that never reads (or disconnects) does not affect the run or its claim."""

    def __init__(self, queue, keepalive, task):
        self._queue = queue
        self._keepalive = keepalive
        self.task = task

    async def stream(self):
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), self._keepalive)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"   # comment ping; keeps an idle connection open
                continue
            if item is _DONE:
                return
            yield item


class ExtractRunner:
    """Owns the single-flight discover claim and the background run tasks.

    The lock's lifetime is the CHILD's lifetime, not the request's: a started run is
    supervised by a background task held in `_tasks`, so a client disconnect tears down
    only the stream. discover is single-flight (one at a time); dry-run bypasses the
    claim (free, no writes) and may run alongside a discover."""

    def __init__(self, spawn=None):
        self.spawn = spawn or _default_spawn
        # The single-flight claim. A plain bool flipped with NO await between the read
        # and the set is atomic under one event loop — unlike a split locked()/acquire(),
        # it cannot let a second discover slip through.
        self._discover_active = False
        # Strong refs to the background tasks. A bare create_task() can be GC'd
        # mid-flight; the disconnect-surviving run is the most exposed. A set (not one
        # slot) because a dry-run can run concurrently with a discover.
        self._tasks = set()
        self.run_timeout = config.EXTRACT_RUN_TIMEOUT_SECONDS
        self.grace = config.EXTRACT_TERMINATE_GRACE_SECONDS
        self.keepalive = config.EXTRACT_SSE_KEEPALIVE_SECONDS

    async def start(self, mode):
        """Claim (discover only), spawn the run, and root its supervise task. Returns
        a _Handle. Raises ExtractBusy if a discover is already in flight."""
        flag = MODE_FLAGS[mode]
        claimed = False
        if mode == "discover":
            if self._discover_active:        # check ...
                raise ExtractBusy()
            self._discover_active = True     # ... and claim, with NO await between
            claimed = True
        try:
            child = await self.spawn(flag)
        except BaseException:
            if claimed:                      # spawn failed before any task owns the claim
                self._discover_active = False
            raise
        queue = asyncio.Queue()              # unbounded: put_nowait(_DONE) cannot raise
        task = asyncio.create_task(self._supervise(child, queue, claimed))
        self._tasks.add(task)                # root it to the runner, not the request
        task.add_done_callback(self._on_task_done)
        return _Handle(queue, self.keepalive, task)

    def _on_task_done(self, task):
        self._tasks.discard(task)
        if task.cancelled():
            return                           # .exception() would raise CancelledError
        exc = task.exception()
        if exc is not None:                  # surface a crashed supervise (and retrieve it)
            print(f"extract: run task crashed: {exc!r}", file=sys.stderr)

    async def _supervise(self, child, queue, owns_claim):
        result = None
        try:
            try:
                result = await asyncio.wait_for(
                    self._pump(child, queue), self.run_timeout)
            except asyncio.TimeoutError:
                await self._terminate(child)         # wedged run -> SIGTERM/grace/SIGKILL
                result = (config.EXIT_OTHER, "timeout")
            await queue.put(result_event(*(result or (config.EXIT_OTHER, "other"))))
        finally:
            # Release FIRST (a bool assign cannot raise, so it is never shadowed by a
            # later enqueue), THEN signal end-of-stream on the unbounded queue.
            if owns_claim:
                self._discover_active = False
            queue.put_nowait(_DONE)

    async def _pump(self, child, queue):
        """Relay stderr lines as log events; capture the PIPELINE_RESULT line as the
        result (not echoed as a log). Returns the parsed (code, reason) or None when the
        contract line never appeared. The child's raw exit code is reaped but
        deliberately NOT mapped to a reason (wrapper=1, argparse=2==EXIT_NETWORK)."""
        result = None
        while True:
            line = await child.stderr.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip("\r\n")
            parsed = parse_pipeline_result(text)
            if parsed is not None:
                result = parsed
            else:
                await queue.put(log_event(text))
        await child.wait()
        return result

    async def _terminate(self, child):
        """SIGTERM, then SIGKILL only if the child ignores it through the grace window.
        Using wait_for(child.wait(), grace) reaps a child that dies during grace
        promptly, so we never SIGKILL a corpse."""
        child.terminate()
        try:
            await asyncio.wait_for(child.wait(), self.grace)
        except asyncio.TimeoutError:
            child.kill()
            await child.wait()


# The app imports this singleton; tests build their own ExtractRunner instances.
runner = ExtractRunner()
