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

# Back up the irreplaceable seed before any run that mutates it. backup_database.py
# opens the live DB read-only and writes a verified, WAL-safe snapshot; if it fails
# we abort WITHOUT running the pipeline (no run without a restore point). A --dry-run
# makes no API calls and no DB writes, so it skips the backup; a not-yet-created DB
# (fresh first run) has nothing to back up.
skip_backup=false
for arg in "$@"; do
  [[ "$arg" == "--dry-run" ]] && skip_backup=true
done

DB_FILE="$(.venv/bin/python -c 'import config; print(config.DB_PATH)')"
if [[ "$skip_backup" == true ]]; then
  echo "Dry run: skipping pre-run backup (no DB writes)."
elif [[ ! -f "$DB_FILE" ]]; then
  echo "No database at $DB_FILE yet; skipping backup (nothing to back up)."
else
  echo "Backing up the seed before the run..."
  if ! .venv/bin/python backup_database.py --dest data/database/backups; then
    echo "ERROR: pre-run backup failed; aborting without running the pipeline." >&2
    exit 1
  fi
fi

exec .venv/bin/python swipefile.py "$@"
