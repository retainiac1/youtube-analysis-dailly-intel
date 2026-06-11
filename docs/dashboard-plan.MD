# daily-intel Dashboard: Build Plan (v1, CC-ready)


## READ ME FIRST - Current Status (created 2026-06-08)

### What this is
A dynamic dashboard over `swipefile.db`: a FastAPI backend serving a static
HTML/JS frontend that visualizes the discovery pipeline's data, lets the user
edit per-video notes, and displays a per-run LLM interpretation. The pipeline
(swipefile.py) is unchanged by this work except for additive `db.py` helpers and
one additive schema bump.

### Where we are
Planning complete, all architecture decisions locked (below). Nothing built.
Phase 0 is fully specced and ready to hand to CC. Phases 1-4 are scoped here at
the level needed to keep them in view; each gets its own detailed spec pass
before it is built, after the prior phase's STOP gate clears.

### Amendment (Phase 3 generator, 2026-06-10)
This plan originally declared interpretations strictly read-only in the dashboard
("the app has no write path at all"). The interpretation-generator feature (see
`docs/interpretations-generator-plan.MD`) deliberately amends that: the dashboard
now has **exactly one** interpretation-write path — `POST /api/interpret`, which
runs synthesis for the viewed lane via the importable generator core and persists
the canonical `interpretations` row + an `llm_invocations` log row — plus the
read-only `GET /api/interpret-defaults` that prepopulates the generate controls.
The outbound LLM call and its keys live entirely server-side. **Everything else in
the dashboard remains read-only**; the read-only-strict statements below are
preserved as written and individually marked `(amended: see Phase 3 generator
amendment)` so the contract history stays honest.

### Architecture decisions (locked)
- Backend: FastAPI + Python, reusing `db.py` helpers. No new connection or
  transaction code.
- Frontend: a single static HTML/JS page served by FastAPI, hitting JSON
  endpoints. Charts via a JS lib (Chart.js or similar). No build step, no SPA.
- Interpretation: stored in a new `interpretations` table keyed
  `(run_date, scope)`, where `scope` is a bucket value (`health` / `habit` /
  `overall`). Updated every run. Displayed read-only by the dashboard.
- The dashboard does NOT generate interpretations. An external, synthesize-style
  step writes them (separate workstream, out of scope here). The dashboard only
  reads and displays them. *(amended: see Phase 3 generator amendment — the
  dashboard now also triggers synthesis via `POST /api/interpret`, calling the
  shared generator core; it does not reimplement generation.)*
- Read-mostly: the ONLY writes the dashboard performs are `videos.user_notes`
  and `videos.starred` (Phase 3). Everything else is read-only. *(amended: see
  Phase 3 generator amendment — also `POST /api/interpret` writes one
  `interpretations` row + one `llm_invocations` row per run.)*
- Location: a `dashboard/` package in the project root
  (`/Users/maryjonesm1/claude-cowork/daily-intel/dashboard/`).
- New deps: `fastapi`, `uvicorn`, and `httpx` (test only, for `TestClient`).
- Visual design: the Glass Theme from `design-files/design-system.md` (dark
  glassmorphism, cyan-to-blue gradient, Outfit/Satoshi). See the Design Standards
  section for the dashboard-specific translation.

### Invariants - violating these corrupts data or breaks the read contract
- NEVER back up a WAL-mode DB with a bare `cp` of the `.db`. `swipefile.db` runs in
  WAL mode, so uncheckpointed state (recent rows, the `user_version` stamp) lives
  in the `-wal`/`-shm` sidecars; copying only the `.db` yields a STALE, invalid
  restore point that silently drops that work. Back up via `VACUUM INTO 'dest.db'`
  (preferred: one consistent file, no sidecars), the sqlite `.backup` API, or
  `PRAGMA wal_checkpoint(TRUNCATE)` then copy all three files together. Verify any
  backup before trusting it: `PRAGMA user_version`, `PRAGMA integrity_check`, and
  row counts must match the live DB.
- The schema bump is ADDITIVE ONLY. The seed data in `swipefile.db` is preserved
  (it is the dashboard's whole point). No table is dropped, altered, or
  re-created destructively; no row is rewritten by the migration.
- The dashboard is the SOLE writer of `user_notes` and `starred`. The pipeline
  never names those columns in any upsert (the user-column contract in
  `VIDEO_USER_COLUMNS`), so there is no write conflict. The dashboard must never
  write any API-owned column.
- `run_date` (Eastern, from `rankings`) and `pacific_date` (Pacific, from
  `quota_ledger`) are different keys for different purposes. NEVER join or equate
  them. The quota widget derives the Pacific date only via the existing
  `config.pacific_date()`.
- Every dashboard DB read goes through `db.run_with_db_retry` so a pipeline
  write-lock degrades to a short retry, not a 500. WAL is already enabled in
  `db.get_connection`, so concurrent pipeline-write / dashboard-read is safe.
- The schema declares NO foreign keys. Joins (`rankings.video_id` ->
  `videos.video_id`, `videos.channel_id` -> `channels.channel_id`) are plain
  joins with no FK enforcement; do not assume referential integrity, LEFT JOIN
  and tolerate a missing parent.
- `buckets` and `matched_queries` are delimited TEXT, not relational columns
  (the delimiter is `|`, per `compute_rankings`). Any filter on them parses in
  the backend; never `WHERE buckets = ?`.
- The dashboard MUST NOT import swipefile.py. swipefile.py pulls in `dotenv`,
  `googleapiclient`, and `openpyxl` at module top, so importing it drags the
  whole API/network dependency tree into the dashboard process. The dashboard
  imports only `db.py` and `config.py` (both stdlib-only). Any pipeline constant
  the dashboard needs (currently the distribution-bucket thresholds) must live in
  a dep-light shared module first. See the shared-constants step in Phase 2.

### Workflow rules
- Plan-before-execute, one phase at a time, explicit STOP gate after each. CC
  brings the diff for review before the next phase starts.
- No em dashes in any output.
- When this doc and the code disagree, the code wins; flag the drift.


## Objective

Give the discovery pipeline a reading surface: three lane leaderboards with
clickable thumbnails and links, change-over-time visuals per video and per rank,
user-selectable multiselect filters, inline note editing on `user_notes`, a
quota-today widget, and a read-only per-run LLM interpretation. All served
locally from `swipefile.db` with no impact on the pipeline's correctness.


## Visualizations catalog (drives Phases 1-2)

Grouped by data source so each one's cost is visible.

### Per-video over time (`stats_snapshots`)
- View-count trajectory (line / sparkline per video): the core "is this taking
  off" signal.
- Velocity between consecutive snapshots (delta views / delta time): catches a
  viral mid-climb better than raw views.
- Engagement-rate over time (likes/views, comments/views): whether engagement
  holds as views scale.
- `views_to_subs_ratio` drift over time (the primary ranking metric).

### Ranking movement (`rankings`, dated rows)
- Bump chart per lane: how a video's rank moves across `run_date`s; surfaces
  entrants, climbers, droppers.
- "New this run" / "fell off" badges by diffing the latest `run_date` against the
  prior one within a lane.
- The three lane leaderboards (health, habit, overall): rank, metric_value, and
  the video's current stats.

### Cross-sectional (current snapshot, `videos` + `channels`)
- Views vs subscriber_count scatter (or ratio): small channels punching above
  their weight, the prime swipe-file signal.
- View-count distribution histogram, reusing the shared distribution-bucket
  thresholds (>=100k ... <1k) so it matches the tuning lens. See the shared-
  constants note below: the dashboard must NOT import swipefile.py for these.
- Lane counts (habit is naturally thinner than health per the pipeline doc;
  surface it, do not treat it as a bug).
- Query performance: which `matched_queries` surface the best videos.
- Top channels by appearances / aggregate views; duration-band distribution.

### Ops widgets
- Units-used-today gauge from `quota_ledger` (Pacific date) vs the effective cap
  (`DAILY_QUOTA_LIMIT - SAFETY_BUFFER`).
- Last run mode / status / time from `run_log`; data freshness from
  `videos.last_api_refresh_at`.


## Design Standards

The dashboard matches the Resource Highway / AI Data Whisperers "Glass Theme":
dark glassmorphism, neon cyan-to-blue gradient accents, frosted translucent
panels, Outfit display + Satoshi body type. The brand is the brief and the brief
wins: extract tokens, do not invent a new look.

### Source of truth
- `design-files/design-system.md` (the Glass Theme tokens and component classes).
- `design-files/` screenshots: the Resource Highway home and the HHT home.
- Rule: cite tokens, not adjectives. Copy the `:root` variables and the
  `tokens.css` baseline from design-system.md verbatim into
  `dashboard/static/css/`; do not retype hex values from memory. If the dashboard
  and design-system.md disagree on a value, design-system.md wins.

### Locked tokens (from design-system.md, do not redefine)
- Background base `#0a0f1a`, elevated `#0d1220`. Card bg `rgba(255,255,255,0.06)`
  frosted, hover `rgba(255,255,255,0.10)`.
- Accents: cyan `#00F2A9`, blue `#2D7CFF`, purple `#a855f7`, red `#ef4444`.
  Signature gradient `linear-gradient(135deg, #00F2A9 0%, #2D7CFF 100%)`.
- Text primary `#ffffff`, secondary/muted `#b4bcd0`.
- Type: Outfit (headings), Satoshi (body), line-height 1.6, antialiased.
- Radii 8/12/16/24px, glass blur 16px (dark) / 18px (light), motion
  0.15s/0.3s/0.5s as defined.
- Light mode via `[data-theme="light"]`, applied before paint to avoid a flash,
  persisted in `localStorage` (`site-theme`), with a sun/dark toggle in the
  header (both screenshots show it top-right).

### Brand-to-dashboard translation (the load-bearing part)
A marketing home page and a dense data tool have different jobs. Keep the tokens;
adapt the patterns. These are the rules that keep the look on-brand without
copying marketing-page behavior that hurts a dashboard:
- Drop the marketing rhythm. The `.section { padding: 6rem 2rem }` vertical
  rhythm and the `clamp(2.5rem, 6vw, 4rem)` hero type are for a landing page. The
  dashboard uses tight density: `heading-md`/`heading-sm` for panel titles,
  compact spacing, data first.
- Glass is for CONTAINERS and metric tiles, not for every row. `backdrop-filter:
  blur(16px)` is expensive; applying it to dozens of leaderboard rows will hurt
  scroll performance. Use `.glass-panel` for the filter rail, the interpretation
  panel, and metric/summary tiles (the right-side card cluster in the Resource
  Highway screenshot is the model). Leaderboard rows get a flat elevated
  background with a hover background-color change, NOT the `translateY(-4px)
  scale(1.01)` lift, which is reserved for the metric cards.
- Do not gradient-clip data. Reserve gradient-clipped text (`heading-xl`
  treatment) for the single dashboard title. Stats, table cells, ratios, and
  notes stay solid `text-primary`/`text-secondary` for legibility.
- Fixed lane-to-color map, used identically across every chart and badge so a
  lane reads the same everywhere: health = cyan `#00F2A9`, habit = blue
  `#2D7CFF`, overall = purple `#a855f7`. Reserve red `#ef4444` for negative signal
  only (rank dropped, fell off). Distribution histogram bars use the signature
  cyan-to-blue gradient.
- Thumbnails are the row anchor: 16:9 YouTube thumbnail in a `--radius-md` frame,
  click opens the video (`videos.link`). Do not put the glass-card top-reflection
  highlight line over a thumbnail; it muddies the image.
- Notes and star (Phase 3): inline note input on a `.glass-panel` background with
  an `accent-cyan` focus ring; starred = filled cyan, unstarred = muted outline.

### Quality floor (frontend-design skill, non-negotiable)
- Responsive to mobile: the filter rail collapses to a drawer; leaderboard rows
  reflow to stacked cards.
- Visible keyboard focus on every interactive element (links, multiselects, note
  input, star toggle, run picker). Do not remove focus outlines.
- Respect `prefers-reduced-motion`: drop hover lifts and transitions.
- Contrast: data values use `text-primary`; `#b4bcd0` is for secondary labels
  only. Verify chart axis/label contrast on the `#0a0f1a` base.

### Copy and voice (frontend-design skill)
- Active voice, sentence case, name things by what the user controls (a lane, a
  run, a filter), not by table or column names.
- Empty states are invitations, in the interface's voice: e.g. "No videos ranked
  for this day yet." Errors state what happened and how to fix it, and do not
  apologize or go vague.

### Where the standards bind, by phase
- Phase 0: the placeholder `index.html` pulls in the copied `tokens.css` and
  fonts and sets the dark default, so the baseline shell exists from the start.
- Phase 1: `.glass-panel` filter rail, leaderboard rows + thumbnail anchors, the
  lane-to-color map established, the theme toggle.
- Phase 2: chart palette drawn from the lane map; distribution and metric tiles
  as glass cards.
- Phase 3: the note input and star affordances above.
- Phase 4: the interpretation panel as a `.glass-panel`; the run picker styled as
  a themed select.


## Schema change (one additive bump, Phase 0)

`db.SCHEMA_VERSION` 2 -> 3 (the code is already at 2; a `categories` table was
added at v2 after this doc was first written, so the interpretations bump is the
2 -> 3 step). Add to `SCHEMA_STATEMENTS`:

```sql
CREATE TABLE IF NOT EXISTS interpretations (
    run_date TEXT,
    scope TEXT,
    text TEXT,
    model TEXT,
    generated_at TEXT,
    PRIMARY KEY (run_date, scope)
)
```

`scope` holds a bucket value, so the table joins to `rankings` on
`run_date = run_date AND scope = bucket`. One row per bucket per run_date (up to
three per day). Because the key is `(run_date, scope)` and the pipeline
overwrites `rankings` by `(run_date, bucket)`, a same-day re-run overwrites both
the rankings and the interpretation in lockstep; they never drift out of grain.

No migration branch is needed. `init_db` already loops `CREATE TABLE IF NOT
EXISTS` over both fresh and seeded DBs and stamps `user_version` when it differs,
so the v2-to-v3 step is the IF-NOT-EXISTS create plus the existing version stamp.
Seed data is untouched.


## Phases

Each phase ends in a STOP. CC builds one phase, brings the diff, waits for
review. Phases 1-4 are scoped here; each gets a detailed spec pass at its gate.

### Phase 0: schema + read API, no UI

Goal: land the schema change and prove the read path end to end. No UI, no
filters, no writes, no time-series. One endpoint per table family the dashboard
depends on, so the read architecture and the new table are both exercised.

Files touched:
- `db.py` (edits, all additive): bump `SCHEMA_VERSION` to 3; add the
  `interpretations` CREATE statement; add three read helpers.
- `dashboard/` (new): `app.py`, a placeholder `static/index.html`, and tests.

`db.py` read helpers (reuse `get_connection`; no new connection/transaction code):
- `fetch_run_dates(conn)`: distinct `run_date` from `rankings`, descending.
- `fetch_lane(conn, run_date, bucket)`: `rankings` LEFT JOIN `videos` on
  `video_id`, returning rank, metric_value, title, channel_title, link,
  thumbnail_url, view_count, views_to_subs_ratio. `rankings.bucket` is a single
  value, so the delimited-TEXT problem does not apply here.
- `fetch_interpretation(conn, run_date, scope)`: the row or None.

NOTE: `upsert_interpretation` is DEFERRED. It belongs to the external generator
workstream (Phase 4 reference), not Phase 0. Phase 0 is read-only-strict: the
app has no write path at all. *(amended: see Phase 3 generator amendment.
`upsert_interpretation` and `log_invocation` now exist and the dashboard DOES
call them, but only through the one `POST /api/interpret` write path; Phase 0's
read endpoints stayed read-only.)*

FastAPI skeleton (`dashboard/app.py`):
- A per-request DB dependency that opens via `db.get_connection` and closes in a
  `finally`. Every read wrapped in `db.run_with_db_retry`.
- Static mount serving a placeholder `index.html` (proves serving only; real UI
  is Phase 1).
- Read-only JSON endpoints:
  - `GET /api/runs`: available `run_date`s.
  - `GET /api/rankings?run_date=&bucket=`: one lane's leaderboard rows.
  - `GET /api/quota`: units used today vs cap. Reuses `config.pacific_date()` and
    `db.get_units_used`; cap = `DAILY_QUOTA_LIMIT - SAFETY_BUFFER`. No new date
    math.
  - `GET /api/interpretation?run_date=&scope=`: stored text, or empty when absent.

Tests (pytest, temp / in-memory DB, no network; mirrors the pipeline suite):
- Fresh DB: `init_db` creates `interpretations` and stamps `user_version = 3`.
- Seeded-prior-version DB (load-bearing): construct a real v2 schema (including
  the existing `categories` table, not a stripped stand-in) with
  `user_version = 2` and a sample `videos` row, run `init_db`, assert
  `interpretations` now exists, `user_version = 3`, and the pre-existing row is
  byte-for-byte unchanged. This is the proof the 2-to-3 migration cannot harm the
  seed.
- Update the existing `testing/test_db.py` inventory: add `interpretations` to
  the expected tables/columns and change the version assertion to `== 3`. These
  break on the bump unless updated, and the fix is in-scope for Phase 0.
- Read helpers return expected rows; `fetch_interpretation` returns None when
  absent.
- Endpoints via FastAPI `TestClient` against a temp DB: runs, rankings (seeded
  video + ranking), quota (seeded ledger row), interpretation (present and
  absent).
- Read-path lock-retry: a read survives a simulated `database is locked`,
  mirroring the existing db-locked retry test.

STOP.

### Phase 1: core read dashboard

Goal: the real UI for the three lane leaderboards plus the multiselect filters.

Scope:
- Replace the placeholder page with the leaderboard view: thumbnail-anchored
  rows, click-through links to YouTube (`videos.link`), rank, metric, current
  stats, per lane.
- Multiselect filters: lane/bucket, `matched_queries`, channel, country,
  published / first_seen date window, view and ratio ranges, duration band,
  starred-only, has-notes-only.
- Filter metadata endpoint(s) to populate the multiselects from the DB.

Watch items:
- `buckets` and `matched_queries` are delimited TEXT. Multiselect on them parses
  and matches in the backend, never a plain equality WHERE. Decide the delimiter
  contract by reading how swipefile.py writes them, do not assume.
- Filtering is combinatorial; keep it server-side over the bounded pool (the pool
  is discovery-bounded, so full scans are cheap, same assumption as
  `fetch_ranking_pool`).

Tests: filter parsing for delimited columns; each filter narrows correctly;
combined filters AND correctly; empty result is a clean empty list, not an error.

STOP.

### Phase 2: change-over-time visuals

Goal: the time-series and distribution charts.

Scope:
- Shared-constants move (do this FIRST, it is a prerequisite). Move
  `DISTRIBUTION_BUCKETS` and `distribution_buckets()` out of swipefile.py into a
  dep-light shared module. config.py is the recommended home (stdlib-only, the
  dashboard already imports it); a new small `constants.py` is the alternative.
  swipefile.py then imports them back from there. This is an additive,
  behavior-preserving refactor of the pipeline: the existing pipeline tests must
  stay green and the bucket thresholds must not change. The point is so the
  dashboard can reuse the exact thresholds WITHOUT importing swipefile.py (see
  the no-swipefile-import invariant).
- Per-video view and velocity trajectory from `stats_snapshots` (one series per
  selected video).
- Rank bump chart across `run_date`s per lane from `rankings`.
- View-count distribution histogram using the shared `distribution_buckets()`
  from its new home, so the dashboard buckets match the pipeline's tuning view
  exactly. Do NOT hand-redefine the thresholds and do NOT import swipefile.py.
- Endpoints feeding each chart as JSON.

Files touched:
- swipefile.py + config.py (or new constants.py): the additive constant move
  above. This is the only pipeline edit in the whole dashboard build.
- `db.py`: new read helpers for the snapshot series and bump-chart data.
- `dashboard/`: the chart UI and endpoints.

Watch items:
- The constant move touches the pipeline, so re-run the full pipeline suite after
  it and confirm green before building any chart on top.
- Snapshot series can be sparse or single-point (a video seen once). Charts must
  render a one-point series without erroring.
- Velocity divides by a time delta; guard a zero/negative delta (clock or
  same-instant snapshots) the same way the pipeline guards its metric math.

Tests: the moved `distribution_buckets()` produces identical output to the
pre-move version for a known set (parity), and the pipeline suite stays green;
snapshot series shape (multi-point, single-point, empty); bump-chart data across
multiple `run_date`s; histogram bucket counts match `distribution_buckets` for a
known set.

STOP.

### Phase 3: writes (notes + starred)

Goal: the only mutations the dashboard performs.

Scope:
- Inline edit of `videos.user_notes`; toggle of `videos.starred` (and
  `starred_at` set on star).
- Write endpoints that update ONLY those columns. Wrap them with
  `db.run_with_db_retry` as the OUTER wrapper and `db.transaction` as the INNER
  one, so a lock rolls the write back and then the retry re-runs the whole
  transaction. Inverting them (transaction outside retry) means the retry never
  sees the lock and the rollback fires too late; do not invert.

Watch items:
- The write SET list names user-owned columns ONLY. Asserting this is the core
  guarantee: a dashboard write must never touch an API-owned column, the mirror
  image of the pipeline's contract.
- Concurrency: a dashboard note write and a pipeline stats upsert can overlap.
  They touch disjoint columns and WAL allows it, but the write still goes through
  the retry wrapper.

Tests: a note write persists and leaves every API-owned column unchanged; a
star toggle sets/clears `starred` and `starred_at`; a write survives a simulated
lock; a concurrent-style pipeline upsert of the same video does not clobber the
note (and the note write does not clobber stats).

STOP.

### Phase 4: run picker + interpretation display

Goal: select a run and read its interpretation. Display only.

Scope:
- A run selector driven by `/api/runs`.
- Render the stored interpretation text for the selected `run_date` and the
  viewed lane (`scope`), read-only, via the existing `/api/interpretation`
  endpoint.
- Add `db.upsert_interpretation(conn, run_date, scope, text, model, now)` here so
  the external generator has its writer. The DASHBOARD never calls it; it exists
  for the separate synthesize-style step. Building that step is out of scope (it
  is the interpretation generator workstream, like synthesize.py in the pipeline
  plan). *(amended: see Phase 3 generator amendment — that workstream shipped, and
  the dashboard now DOES call `upsert_interpretation`, via the one
  `POST /api/interpret` endpoint built on the generator core.)*

Watch items:
- Absent interpretation (a run with no generated text yet) renders as a clean
  empty state, not an error.

Tests: picker lists run_dates; display renders present text and the empty state;
`upsert_interpretation` inserts then overwrites on the same `(run_date, scope)`
without touching other rows.

STOP.


## Out of scope (v1)

- The interpretation generator itself (the external synthesize-style step that
  writes `interpretations` rows). The dashboard only displays its output.
  *(amended: see Phase 3 generator amendment — the generator was built, and the
  dashboard now triggers it for the viewed lane via `POST /api/interpret`.)*
- Authentication / multi-user. This is a local single-user tool.
- Editing API-owned data of any kind from the dashboard.
- Scheduling or triggering pipeline runs from the dashboard.
- Per-YouTube-category interpretation (explicitly dropped: interpretation is
  keyed to the lane/bucket grain, less granular than `videos.category_id`).


## Decisions: all resolved
- Backend FastAPI + Python, reusing db.py. Frontend static HTML/JS, no build.
- Interpretation table `(run_date, scope)`, scope = bucket, updated every run,
  read-only in the dashboard, written by an external step. *(amended: see Phase 3
  generator amendment — the dashboard now also writes it via `POST /api/interpret`.)*
- Dashboard writes only `user_notes` and `starred`. *(amended: see Phase 3
  generator amendment — plus `interpretations` + `llm_invocations` via the one
  `POST /api/interpret` write path.)*
- Schema bump 2 -> 3, additive, seed preserved, via the existing user_version
  hook.
- `upsert_interpretation` deferred to Phase 4 (for the external generator).
  - Deps fastapi, uvicorn, httpx (test). Location dashboard/ in project root. 