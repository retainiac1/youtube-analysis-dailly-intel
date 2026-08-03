#!/usr/bin/env bash
#
# Run the daily-intel discovery/ranking pipeline (swipefile.py).
#
# All arguments are passed straight through to swipefile.py, which defines the
# real CLI (a mutually-exclusive mode group). Common uses:
#
#   scripts/run-pipeline.sh              # auto: discover if none today, else refresh
#   scripts/run-pipeline.sh --discover   # force a discovery run (expensive)
#   scripts/run-pipeline.sh --refresh    # force a cheap stats refresh
#   scripts/run-pipeline.sh --dry-run    # print plan + quota estimate, no API calls
#
# Runs from the repo root so load_dotenv() finds .env (YOUTUBE_API_KEY) and the
# relative DB path (data/database/swipefile.db) resolves correctly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

if [[ ! -x .venv/bin/python ]]; then
  echo "ERROR: .venv not found at $REPO_ROOT/.venv" >&2
  echo "Create it and install deps, e.g.:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

# Read the config contract in one shot: the DB path (reused by the backup gate below), the
# discover lock file, and the lock-held exit code. config.py is the single source; nothing
# here is a shell-side magic number.
config_line="$(.venv/bin/python -c 'import config; print(config.DB_PATH, config.DISCOVER_LOCK_PATH, config.EXIT_DISCOVER_LOCKED)')" || {
  echo "ERROR: could not read config (DB_PATH / lock settings); aborting." >&2
  exit 1
}
read -r DB_FILE LOCK_FILE EXIT_LOCKED <<<"$config_line"

# --- Cross-process discover lock -------------------------------------------------
# The dashboard's single-flight guard is in-process only. A scheduled launchd fire is a
# separate process, so discover-capable runs (anything that is NOT --dry-run and NOT
# --refresh, i.e. --discover and bare/auto) serialize on an OS advisory lock (flock). We
# open the lock file on fd 9 and KEEP it open for the whole script, then take a non-blocking
# exclusive flock via discover_lock.py. Because the lock lives on the fd this shell holds,
# the kernel releases it automatically when the script exits, errors, is signalled, OR
# crashes - so there is no stale lock to reclaim and no double-acquire race (unlike a
# hand-rolled mkdir/symlink lock, whose reclaim step is not atomic). A second discover gets
# EXIT_LOCKED and skips: a skipped fire, no run_log row (it never reaches swipefile.py),
# rather than launching a duplicate expensive run and double-spending quota.
takes_lock=true
for arg in "$@"; do
  case "$arg" in
    --dry-run|--refresh) takes_lock=false ;;
  esac
done

if [[ "$takes_lock" == true ]]; then
  mkdir -p "$(dirname "$LOCK_FILE")"
  # fd 9 stays open for the rest of the script; closing it (on any exit) frees the lock.
  exec 9>"$LOCK_FILE"
  lock_rc=0
  .venv/bin/python scripts/discover_lock.py 9 "$EXIT_LOCKED" || lock_rc=$?
  if [[ "$lock_rc" -eq "$EXIT_LOCKED" ]]; then
    echo "ALERT: another discover is already in flight (lock held on $LOCK_FILE); skipping this run." >&2
    exit "$EXIT_LOCKED"
  elif [[ "$lock_rc" -ne 0 ]]; then
    # Helper usage/OS error: a shell-gate failure that never reached the pipeline, like a
    # failed backup below. Exit 1, the code reserved for exactly that.
    echo "ERROR: discover lock helper failed (rc=$lock_rc); aborting." >&2
    exit 1
  fi
fi

# Back up the irreplaceable seed before any run that mutates it. backup_database.py
# opens the live DB read-only and writes a verified, WAL-safe snapshot; if it fails
# we abort WITHOUT running the pipeline (no run without a restore point). A --dry-run
# normally makes no API calls and no DB writes, so it skips the backup; a not-yet-
# created DB (fresh first run) has nothing to back up. EXCEPTION: if a schema
# migration is pending, back up regardless of mode; the dry-run path is read-only
# and won't apply it, but this is belt-and-suspenders so no migration-applying run
# can ever skip the gate.
skip_backup=false
for arg in "$@"; do
  [[ "$arg" == "--dry-run" ]] && skip_backup=true
done

pending_migration=false
if .venv/bin/python -c 'import sys, config, db; sys.exit(0 if db.migration_pending(config.DB_PATH) else 1)'; then
  pending_migration=true
fi

if [[ "$skip_backup" == true && "$pending_migration" == false ]]; then
  echo "Dry run: skipping pre-run backup (no DB writes)."
elif [[ ! -f "$DB_FILE" ]]; then
  echo "No database at $DB_FILE yet; skipping backup (nothing to back up)."
else
  if [[ "$pending_migration" == true ]]; then
    echo "Schema migration pending; backing up the seed before the run..."
  else
    echo "Backing up the seed before the run..."
  fi
  if ! .venv/bin/python backup_database.py --dest data/database/backups; then
    echo "ERROR: pre-run backup failed; aborting without running the pipeline." >&2
    exit 1
  fi
fi

# Run the pipeline and PRESERVE its contract exit code for the scheduler. Not
# `exec`: we branch on the code first so the fallback-spend breaker (exit 8, net-new
# to the 2..7 set) surfaces as a distinct, greppable operational alert rather than a
# silent net-new code. `|| exit_code=$?` captures a non-zero exit without tripping
# `set -e`; every other code passes through unchanged.
exit_code=0
.venv/bin/python swipefile.py "$@" || exit_code=$?
if [ "$exit_code" -eq 8 ]; then
  echo "ALERT: classify fallback-spend breaker tripped (exit 8): Gemini throttled, per-run Haiku cap reached" >&2
fi
exit "$exit_code"
