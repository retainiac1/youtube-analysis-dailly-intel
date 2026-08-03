#!/usr/bin/env python3
"""Non-blocking cross-process discover lock for run-pipeline.sh.

run-pipeline.sh opens the lock file on a fixed shell fd and keeps it open for the whole
run, then calls this helper to take a NON-BLOCKING exclusive flock on that inherited fd.

Why flock on an inherited fd instead of a hand-rolled mkdir/symlink lock: the lock lives on
the open file description the shell holds, so the kernel releases it automatically when the
shell exits OR crashes (or is SIGKILLed). That removes the entire stale-reclaim problem and
its races - there is no lock file to leave behind, no PID/timestamp to validate, no window
where a half-built lock reads as abandoned. The helper only has to TAKE the lock; the shell
holding the fd is what KEEPS it, for exactly the run's lifetime.

Usage: discover_lock.py <fd> <locked_exit_code>
  <locked_exit_code> is passed in by the caller (which reads it from config, the single
  source for the exit-code contract) so this helper stays dependency-free and does not have
  to import the project package from a subdirectory.

Exit codes:
  0                  acquired; the shell now holds it via the open fd.
  <locked_exit_code> another discover already holds it; caller should skip this run.
  1                  bad usage or an unexpected flock error; caller should abort loudly.
"""
import fcntl
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: discover_lock.py <fd> <locked_exit_code>", file=sys.stderr)
        return 1
    try:
        fd = int(argv[1])
        locked_code = int(argv[2])
    except ValueError:
        print(f"discover_lock: fd and exit code must be integers, got {argv[1:]!r}",
              file=sys.stderr)
        return 1
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Held by another discover. A skipped fire, not a failure.
        return locked_code
    except OSError as exc:
        print(f"discover_lock: flock failed on fd {fd}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
