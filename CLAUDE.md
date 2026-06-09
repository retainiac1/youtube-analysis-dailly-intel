# daily-intel — operating rules for Claude

Project context and niche/brand rules live in `context.MD`. The dashboard build
plan and its invariants live in `docs/dashboard-plan.MD`. Read those before
working in their areas.

## Data safety (load-bearing)

- `data/database/swipefile.db` is the irreplaceable seed for the dashboard. Back it
  up before any destructive or schema-changing operation, and verify the backup.
- It runs in **WAL mode**. NEVER back it up with a bare `cp` of the `.db` file:
  uncheckpointed state (recent rows, the `user_version` stamp) lives in the
  `-wal`/`-shm` sidecars, so a lone `.db` copy is a STALE, invalid restore point
  that silently loses work. Back up with one of:
  - `sqlite3 data/database/swipefile.db "VACUUM INTO 'dest.db'"` (preferred — one
    consistent file, no sidecars), or
  - the sqlite `.backup` API, or
  - `PRAGMA wal_checkpoint(TRUNCATE)` then copy all three files (`.db`, `-wal`,
    `-shm`) together.
  Then verify before trusting it: `PRAGMA user_version`, `PRAGMA integrity_check`,
  and row counts must match the live DB.

## Verification discipline

- When the plan says run a smoke against a COPY, run it against a copy — point the
  dashboard at one via `DASHBOARD_DB_PATH`, never the live seed.
- `PRAGMA wal_checkpoint` (even PASSIVE) WRITES to the main DB file. It is not a
  read-only inspection; do not run it when only reads are authorized.
