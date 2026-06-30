#!/usr/bin/env bash
#
# Start the daily-intel FastAPI dashboard (uvicorn).
#
#   scripts/start-dashboard.sh            # dev mode (default): auto-reload on edits
#   scripts/start-dashboard.sh --dev      # same as above
#   scripts/start-dashboard.sh --prod     # no reload; stable always-on local instance
#
# Both modes bind to 127.0.0.1 (loopback only) — never 0.0.0.0, so the dashboard
# is not exposed on the network. The only difference between modes is --reload.
#
# Env overrides:
#   HOST=...                bind address (default 127.0.0.1)
#   PORT=...                port (default 8000)
#   DASHBOARD_DB_PATH=...   point the app at a specific DB (e.g. a smoke copy);
#                           inherited by the app via get_db_path(). Leave unset to
#                           use the live seed at data/database/swipefile.db.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

if [[ ! -x .venv/bin/uvicorn ]]; then
  echo "ERROR: .venv/bin/uvicorn not found at $REPO_ROOT/.venv" >&2
  echo "Create the venv and install deps, e.g.:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

MODE="dev"
case "${1:-}" in
  --dev|"") MODE="dev" ;;
  --prod)   MODE="prod" ;;
  *)
    echo "ERROR: unknown argument '$1' (expected --dev or --prod)" >&2
    exit 1
    ;;
esac

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

# Stop any dashboard already running so a stale process serving old code can't
# linger (a frozen process keeps the new routes but an old db/config in memory).
# Match the app target, not the port, so unrelated listeners are never killed.
# This matches both the --reload parent and its worker (both carry the target in
# their command line); pkill never signals the running script itself.
if pgrep -f "uvicorn dashboard.app:app" >/dev/null 2>&1; then
  echo "Stopping running dashboard..."
  pkill -f "uvicorn dashboard.app:app" || true
  # Wait for the port to free up before rebinding (avoid "address already in use").
  if command -v lsof >/dev/null 2>&1; then
    for _ in $(seq 1 20); do
      lsof -ti "tcp:$PORT" >/dev/null 2>&1 || break
      sleep 0.25
    done
  else
    sleep 1
  fi
fi

reload_args=()
if [[ "$MODE" == "dev" ]]; then
  # Watch the WHOLE repo root, not just dashboard/: the backend modules (db.py,
  # config.py, llm.py, classify.py, ...) live at the root, so scoping reload to
  # dashboard/ silently served stale Python after a db.py edit. uvicorn only
  # reloads on *.py by default, so DB/WAL and log writes never trigger a reload.
  # The excludes keep the watcher off .venv/.git/data/logs; they MUST be absolute
  # paths because uvicorn's FileFilter checks `exclude_dir in path.parents` against
  # the ABSOLUTE paths watchfiles reports (a relative exclude never matches and the
  # dir reloads anyway). Needs watchfiles (in requirements.txt); without it uvicorn
  # falls back to polling and these excludes no-op.
  reload_args+=(--reload
    --reload-dir "$REPO_ROOT"
    --reload-exclude "$REPO_ROOT/.venv"
    --reload-exclude "$REPO_ROOT/.git"
    --reload-exclude "$REPO_ROOT/data"
    --reload-exclude "$REPO_ROOT/logs")
fi

echo "Dashboard → http://$HOST:$PORT ($MODE)"
# ${arr[@]+...} guards against bash 3.2 (macOS) erroring on an empty array under set -u.
exec .venv/bin/uvicorn dashboard.app:app --host "$HOST" --port "$PORT" ${reload_args[@]+"${reload_args[@]}"}
