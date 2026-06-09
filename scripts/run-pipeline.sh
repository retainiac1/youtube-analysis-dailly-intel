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

exec .venv/bin/python swipefile.py "$@"
