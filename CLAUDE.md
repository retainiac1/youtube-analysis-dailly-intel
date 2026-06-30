# daily-intel — operating rules for Claude

Project context and niche/brand rules live in `context.MD`. Build plans and their
invariants live in `docs/`. Read the relevant plan before working in its area.

## How we work (load-bearing)

- Plan before execute. For any non-trivial change, write an implementation plan
  with file:line anchors, STOP gates, and verify steps. Get it reviewed BEFORE
  writing code. Do not code through a gate without sign-off.
- One gate at a time. Finish a gate (code, verify, commit) before starting the
  next. Do not run two workstreams against the same files in parallel.
- Treat all cited line numbers as approximate. Re-grep every anchor immediately
  before editing; the file has shifted. BSD grep (macOS) needs `-E`, not `\|`.
- Deviate-and-flag only when a plan contradicts a locked constraint and there is
  one correct resolution. Stop-and-ask when the choice is a product call or a new
  tradeoff. Never silently change a shipped path.
- Re-check the branch (`git rev-parse --abbrev-ref HEAD`) and stage explicit paths
  right before every commit. No Co-Authored-By trailer.

## Code and content rules

- No em dashes anywhere: code, comments, commit messages, UI strings, captions,
  null placeholders. Use a hyphen, "to" for ranges, or restructure.
- No magic numbers or literals. Tunables live in `settings.toml` + `config.py`;
  coupled sets (bin edges, bands) are defined once as a named constant, never
  inline.
- Single source of truth. A value or predicate is defined in one place and reused.
  When a count and a drill (or two charts) must agree, they share the same code,
  not parallel reimplementations.
- Deterministic over LLM. Prefer computed logic to model calls where either works.
- Eastern, instant-correct. Order or subtract timestamps via `_parse_instant`,
  never raw-string compares. `run_date` is a zero-padded date key compared
  lexically by design; that exception stays.

## Schema and database

- Declare "no schema change" explicitly in any plan that touches `db.py`. Do not
  bump `SCHEMA_VERSION`, call `init_db`, add a migration, or touch the backup gate
  unless the plan is explicitly a schema change. The `== 14` asserts in
  `testing/test_db.py` and `testing/test_dashboard_db.py` must stay green.
- Stop the dev server before any `db.py` schema edit. With `--reload-dir` at the repo
  root, editing `db.py` reloads the server and its startup `init_db` will migrate
  whatever DB it points at, including the live seed, bypassing the backup gate. (The
  lifespan now refuses to auto-migrate an existing DB; do not rely on that as the
  gate.) For any schema change: stop the server, back up and verify, run the
  migration deliberately, then restart.
- Bounded SQL only. Never pass an unbounded per-id `IN (...)` list (the deduped
  set is hundreds and grows). For the full population, filter via the rankings
  membership subquery; for a fixed cohort, pass the bounded cohort id list.

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

- When the plan says run a smoke against a COPY, run it against a copy: point the
  dashboard at one via `DASHBOARD_DB_PATH`, never the live seed.
- `PRAGMA wal_checkpoint` (even PASSIVE) WRITES to the main DB file. It is not a
  read-only inspection; do not run it when only reads are authorized.
- `immutable=1` is valid ONLY on a VACUUM INTO copy (no `-wal` sidecar), NEVER on
  the live WAL file: immutable reads skip the sidecar and return stale data.
- Reviews of UI work lead with before/after screenshots from a real browser
  (playwright against a copy). A screenshot is the verification; a description is
  not.

## Frontend and UI work

Any task that creates or changes UI, charts, layout, CSS, or visual output MUST use
the frontend-design skill and actually apply its judgment, not just invoke it.
Invoking the skill while reusing house defaults does not count.

Before building any chart or view, state in the plan:
- The one question this view answers for the reader.
- The axis, label, and legend choices in plain reader language.
- The single thing being optimized for legibility.

Hard rules, learned the hard way:
- Intuitive without explanation. Every axis gets a plain-words title. Legends stay
  short. Card titles name what the chart shows, not how the cohort was selected
  (selection goes in a caption). Truncate titles emoji-safe (never split a
  surrogate pair).
- Do not chart an artifact of the data source. Confirm a chart shows a real signal,
  not a mechanic of how the data is built. A number forced by construction is not a
  finding; cut it.
- One chart, one job. Do not stack a second metric or scale onto a chart that
  works. If two metrics fight, split them.
- Degenerate means guidance, not a broken-looking chart. When a view would be empty
  or near-empty, render an empty state that tells the reader what to do.
- Match the chart to the data's maturity. If there is not enough data for a trend,
  say so instead of drawing phantom trends across gaps.
- Reusing a renderer is not an exception to any of the above. If the reuse hurts
  legibility, write a purpose-specific renderer.
- 