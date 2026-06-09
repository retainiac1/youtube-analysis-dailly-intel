# daily-intel

A YouTube Shorts discovery and ranking pipeline that maintains a SQLite
leaderboard of trending videos and serves a read-only FastAPI dashboard to
browse the results.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # add -r requirements-dev.txt for tests
```

The pipeline needs a `YOUTUBE_API_KEY` in a `.env` file at the repo root. The
dashboard needs no key — it only reads the database.

There is **no database server** to start: the project uses SQLite (an embedded
file at `data/database/swipefile.db`). WAL mode lets the dashboard read while the
pipeline writes.

## Running the services

Two scripts under `scripts/` launch the two processes. Both can be run from any
directory — they resolve the repo root from their own location and exit with a
clear message if `.venv` is missing.

### Pipeline — `scripts/run-pipeline.sh`

Runs the discovery/ranking pipeline (`swipefile.py`). Arguments pass straight
through to the underlying CLI:

```bash
scripts/run-pipeline.sh              # auto: discover if none today, else refresh
scripts/run-pipeline.sh --discover   # force a discovery run (expensive: searches + enrichment)
scripts/run-pipeline.sh --refresh    # force a cheap stats refresh of tracked videos
scripts/run-pipeline.sh --dry-run    # print the plan + quota estimate, no API calls
```

### Dashboard — `scripts/start-dashboard.sh`

Starts the FastAPI dashboard via uvicorn.

```bash
scripts/start-dashboard.sh           # dev mode (default): auto-reload on code edits
scripts/start-dashboard.sh --dev     # same as above
scripts/start-dashboard.sh --prod    # no reload; stable always-on local instance
```

Both modes bind to **`127.0.0.1` (loopback only)** — never `0.0.0.0` — so the
dashboard is not exposed on the network. The default URL is
<http://127.0.0.1:8000>.

Environment overrides:

| Variable            | Default            | Purpose                                                        |
| ------------------- | ------------------ | -------------------------------------------------------------- |
| `HOST`              | `127.0.0.1`        | Bind address.                                                  |
| `PORT`              | `8000`             | Port.                                                          |
| `DASHBOARD_DB_PATH` | live seed DB       | Point the app at a specific DB, e.g. a throwaway smoke copy.   |

To smoke-test against a throwaway copy instead of the live seed (recommended —
never point a smoke run at the live database):

```bash
sqlite3 data/database/swipefile.db "VACUUM INTO '/tmp/smoke.db'"
DASHBOARD_DB_PATH=/tmp/smoke.db scripts/start-dashboard.sh --dev
```
