# YouTube Swipe File: SQLite Refactor Build Plan (v4, CC-ready)


## READ ME FIRST — Current Status (updated 2026-06-08)

### Where we are
Phases 1-3 complete and tested. Phase 4 (run modes + rate limiting) complete and
tested, plus a crash fix and a filter-tuning pass. Suite is green at 83 tests.
The DB (swipefile.db) holds clean, tuned discovery data and is being PRESERVED
as seed data for the dashboard — do not wipe it.

### Open threads (in order)
1. Settings extraction — move user-tunable constants from config.py into a
   human-editable settings.toml (TOML, stdlib tomllib, validated at load).
   Behavior-preserving; data untouched. (Prompt drafted, not yet run.)
2. Change B — untagged-title language detector (fail-closed on low confidence),
   a new language gate for videos with no defaultAudioLanguage tag. NOT built.
3. Phase 4 STOP-gate sign-off.
4. Phase 5 — --export-xlsx (reuses the dormant write_xlsx/format_video_row helpers).
5. Dashboard — separate coding agent, reads the DB (incl. quota_ledger for the
   units-today widget).

### Invariants — violating these causes silent corruption
- Eastern-only DB timestamps via now_local_iso(), offset-bearing ISO-8601,
  NEVER UTC, NEVER naive. Exempt: get_published_after() returns RFC3339-Z because
  it's a YouTube API request param, not stored data.
- rankings.run_date (Eastern) and quota_ledger.pacific_date (Pacific) are
  different keys for different purposes. NEVER join or equate them.
- User-column contract: user_notes / starred / starred_at (and hook /
  first_10_sec / saves) are NEVER named in any upsert SET list. API refreshes
  cannot touch them.
- Parse timestamps to aware datetimes and compare instants. NEVER lexical
  string compare.
- Quota ledger durability: search.list (100-unit) charges flush to quota_ledger
  eagerly in their own committed transaction; cheap calls flush once at run end
  (flushed_units prevents double-count). Protects the under-10k requirement
  across hard kills.

### Tuning rationale (so it isn't re-litigated)
- MIN_VIEWS = 10,000: derived from the live qualifying-view distribution (only 4
  of 549 videos cleared 100k in a 3-day window). It's the floor that fills the
  WEAKER habit lane; health self-caps at TOP_N=20. Do NOT raise it for health's
  sake — that starves habit.
- The habit lane is naturally thinner than health (its Shorts accrue views
  slower). ~10 rows is healthy for habit, not a bug.
- relevanceLanguage='en' was already active during the foreign-language flood,
  so it's a WEAK soft bias that does NOT contain foreign content. The language
  GATE is the real wall; the untagged-title detector (Change B) is load-bearing,
  not cleanup.
- Trending lane was considered and dropped. Three lanes only: health, habit, overall.

### Workflow rules
- Plan-before-execute, one phase at a time, explicit STOP gate after each — wait
  for review before the next.
- No em dashes in any written output.
- When the plan doc self-contradicts, error-handling/test intent wins over
  literal schema.


## Objective

Refactor `swipefile.py` into a persistent, idempotent Shorts pipeline that
maintains top-20 rankings in three lanes (Health & Wellness, Habit, Overall) in
SQLite, runs multiple times per day, updates only changed data, preserves user
commentary and an HHT reuse flag across runs, stays under the daily quota by
design, and exposes daily unit usage to the dashboard.

## Changelog from v3

- Trending lane removed entirely. Three lanes only: Health, Habit, Overall.
  All Trending config, schema enum values, pipeline steps, and tests dropped.
- No-flag run-mode default locked: discover if none succeeded today (Pacific),
  else refresh.
- All decisions resolved. This version is ready for Claude Code.

## Changelog from v2

- Added a full Testing section with concrete unit and integration test cases.
- Added an Error handling section covering API, DB, data, and run safety.
- Updated the Trending lane to reflect that mostPopular now returns only
  Trending Music/Movies/Gaming (YouTube change effective July 21, 2025) and that
  content-type exclusion (e.g. podcasts) is client-side only.

## Changelog from v1

- Overall: `overall` = top 20 across the tracked Shorts pool, ranked by the
  chosen metric. (A global Trending lane was considered and dropped in v4.)
- Ranking metric: primary sort is `views_to_subs_ratio`. Capture all metrics
  (views, velocity, ratio) in the DB and surface them in the dashboard.
- Window: 3 days (the horizon to catch virals, per prior decision).
- Metric history: yes. Latest stats on `videos` AND a `stats_snapshots` row per
  refresh.
- Ranking history: yes. Dated `rankings` rows.
- XLSX: drop per-run timestamped output. Add on-demand `--export-xlsx`.
- New: track units used per day, persist it, show it on the dashboard.
- New: proactive rate limiting to stay under 10,000 units/day.

## Current state (what exists)

`swipefile.py` + `config.py`: flat `SEARCH_QUERIES`, search.list
(`order=viewCount`, `videoDuration=short`) then videos.list, filter, channel and
comment enrichment, one list sorted by `views_to_subs_ratio`, fresh timestamped
XLSX per run, JSON resume state cleared on success, quota tracking, `--dry-run`.

## Quota budget (drives run modes and rate limiting)

Verified YouTube Data API v3 costs:

- Default 10,000 units per project per day, reset at midnight Pacific.
- search.list costs 100 units per call and is the dominant cost.
- videos.list is a read at 1 unit per call, batching up to 50 IDs.
- channels.list and commentThreads.list are 1 unit each.

Discovery (searches) is expensive; stat refresh is nearly free. That split is
why discovery runs once daily and refresh runs cheaply intraday.

## Target SQLite schema

DB path in config (e.g. `swipefile.db`). API-owned fields are upserted;
user-owned fields are never written by a refresh.

### `videos` (one row per unique video)
API-owned: `video_id` (PK), `title`, `channel_id`, `channel_title`,
`published_at`, `duration_seconds`, `is_short`, `link`, `thumbnail_url`,
`description`, `category_id`, `audio_language`, `definition`, `has_captions`,
`made_for_kids`, `tags`, `topic_categories`, `top_comments`, `matched_queries`,
`buckets` (union of lanes the video qualifies for).

Latest stats (refreshed): `view_count`, `like_count`, `comment_count`,
`views_to_subs_ratio`, `views_per_day`.

Bookkeeping: `first_seen_at` (set once via COALESCE), `last_api_refresh_at`
(updated only when an API field changed).

User-owned (never overwritten): `user_notes TEXT DEFAULT ''`,
`starred INTEGER DEFAULT 0`, `starred_at TEXT`. Migrate existing manual fields
`hook`, `first_10_sec`, `saves` as user-owned.

### `channels`
`channel_id` (PK), `subscriber_count`, `channel_video_count`,
`channel_total_views`, `channel_created_date`, `channel_country`,
`channel_keywords`, `last_updated_at`. Upsert by `channel_id`.

### `stats_snapshots`
`id` (PK), `video_id`, `captured_at`, `view_count`, `like_count`,
`comment_count`. One row per refresh per video.

### `rankings`
`run_date`, `bucket` (`health` | `habit` | `overall`),
`rank` (1..20), `video_id`, `metric_value`, `captured_at`.
PK (`run_date`, `bucket`, `rank`).

### `run_log`
`run_id` (PK), `mode` (`discover` | `refresh` | `export`), `started_at`,
`finished_at`, `quota_used`, `videos_seen`, `status`.

### `quota_ledger` (new)
`pacific_date` (PK, `YYYY-MM-DD` in America/Los_Angeles), `units_used`,
`updated_at`. Incremented as units are consumed. The dashboard reads this for
"units used today." Pacific date matters because the Google quota resets at
midnight Pacific, not Eastern.

## Idempotency: the upsert contract

The upsert SET list contains API-owned columns only. User-owned columns are
never named in any UPDATE, so they cannot be clobbered.

```
INSERT INTO videos (video_id, title, ..., first_seen_at, last_api_refresh_at)
VALUES (:video_id, :title, ..., :now, :now)
ON CONFLICT(video_id) DO UPDATE SET
  title = excluded.title,
  view_count = excluded.view_count,
  ...,                       -- API fields only
  last_api_refresh_at = CASE
    WHEN videos.view_count <> excluded.view_count
      OR videos.title <> excluded.title
      OR ...                 -- any API field differs
    THEN :now ELSE videos.last_api_refresh_at END;
```

`first_seen_at` set on insert, never updated. `last_api_refresh_at` moves only
on a real change.

## Rate limiting (stay under 10,000/day)

Two layers, both keyed to the Pacific-date `quota_ledger` row:

- Config: `DAILY_QUOTA_LIMIT = 10000`, `SAFETY_BUFFER` (e.g. 500), so effective
  cap = 9500. `QUOTA_RESET_TZ = "America/Los_Angeles"`.
- Pre-flight: estimate the run's cost (same math as `--dry-run`). If
  `units_used_today + estimate > cap`, refuse `--discover` and log
  `status = "quota_preflight_stop"`. A `--refresh` is cheap and almost always
  clears.
- Per-call guard: inside `api_call_with_retry`, before charging a call, if
  `units_used_today + cost > cap`, abort gracefully and log
  `status = "quota_guard_stop"`. This is proactive; the existing reactive
  `quotaExceeded` handler stays as a backstop.
- Ledger update: increment `quota_ledger.units_used` for today's Pacific date as
  units are consumed (or atomically at run end), in addition to the per-run
  `run_log.quota_used`.

## Run modes

- `--discover` (daily, expensive): bucket-tagged searches, fetch details,
  filter, enrich, upsert, write `stats_snapshots`, recompute all three
  rankings, write `rankings` rows.
- `--refresh` (intraday, cheap): no searches. Re-pull videos.list stats for DB
  videos (batched 50/call), upsert stats, write `stats_snapshots`, recompute
  rankings from refreshed numbers.
- `--dry-run`: planned queries + estimated quota, updated for new lane counts.
- `--export-xlsx`: write one current snapshot to XLSX on demand.
- No-flag default: run `--discover` if none succeeded today (Pacific), else
  `--refresh`. (Assumed; flag if you want a different default.)

## Config changes (`config.py`)

- Replace flat `SEARCH_QUERIES` with bucket-tagged entries, e.g.
  `{"q": "...", "bucket": "health"}`, `{"q": "...", "bucket": "habit"}`.
  `overall` is assigned by ranking the tracked pool.
- Add: `DB_PATH`, `WINDOW_DAYS = 3`, `TOP_N = 20`, `DAILY_QUOTA_LIMIT = 10000`,
  `SAFETY_BUFFER`, `QUOTA_RESET_TZ`.
- Keep: `MIN_VIEWS`, `MAX_DURATION_SECONDS`, blocked categories, quota costs,
  batch sizes. `PUBLISHED_AFTER` becomes `now - WINDOW_DAYS`.

## Error handling (critical)

Principle: a single failed enrichment or a transient error must never lose work
already fetched, corrupt the DB, or leave `run_log` without a terminal status.

### API layer
- Keep the existing `api_call_with_retry` classification: `keyInvalid` exits,
  `quotaExceeded` returns None and aborts cleanly, `commentsDisabled` /
  `forbidden` / `videoNotFound` skip the item, 429 and network errors retry once
  with backoff.
- Add transient 5xx (500, 503) to the retry path with exponential backoff
  (e.g. 2s, 8s) capped at 2 attempts.
- Add the proactive quota guard (pre-flight and per-call) from the rate-limiting
  section as a layer above the reactive `quotaExceeded` handler.
- Treat partial failure as non-fatal: if comment or channel enrichment fails,
  persist the video rows already gathered and continue.

### Data integrity (defensive parsing)
- `statistics.viewCount` and `likeCount` can be absent or hidden. Treat missing
  counts as None, exclude such videos from metric ranking rather than crashing,
  and store what is present.
- Missing `contentDetails.duration` means `is_short` cannot be derived: default
  to excluded and log, do not assume Short.
- Guard all division: `views_per_day` when days-since-publish is 0 (published
  today) and `views_to_subs_ratio` when subscriber count is 0 or hidden.
- Wrap `published_at` parsing; on failure, skip velocity but keep the row.

### DB layer
- Enable WAL mode so the dashboard can read while the script writes.
- Wrap each phase's writes in a transaction; on exception, roll back that phase
  so the DB is never left half-updated.
- Retry on `sqlite3.OperationalError: database is locked` with short backoff
  (dashboard or DB browser may hold a read lock).
- Guard `stats_snapshots` so an interrupted-then-rerun discover does not write
  two snapshots for the same video in the same run (key the write to `run_id`).
- Always close connections in a `finally`.

### Run safety
- Validate config at startup: required keys present, API key loaded, DB path
  writable, query buckets valid. Fail fast with a clear message.
- Wrap the whole run so any unhandled exception still writes `run_log` with
  `status` and `finished_at` before exiting non-zero.
- Any lane returning zero rows after filtering writes zero ranking rows and
  logs it, rather than erroring.

## Testing

Use `pytest`. Pure functions get unit tests with fixtures; DB logic gets
integration tests against an in-memory or temp-file SQLite database. Mock all
YouTube API calls (no live network in the test suite).

### Unit tests (pure functions)
- `parse_duration`: `PT1H2M3S` to 3723, `PT45S` to 45, `PT3M` to 180, empty
  string to 0, malformed input to 0.
- `is_short` derivation: at, just under, and just over the threshold.
- `best_thumbnail`: returns highest-priority key present; empty dict returns "".
- `filter_videos`: made-for-kids dropped, blocked category dropped, non-English
  audio dropped, under-`MIN_VIEWS` dropped, over-duration dropped, a clean video
  kept.
- `dedupe_videos`: same video from two queries merges to one row and accumulates
  both queries; distinct videos stay separate.
- Metric math: `views_per_day` correct, and zero-days and zero-subs cases do not
  raise.
- Bucket assignment: query-to-bucket mapping, multi-bucket union for a video
  that matched both a health and a habit query.
- Ranking selection: exactly `TOP_N` returned when more exist, all returned when
  fewer exist, deterministic tie handling.
- Pacific-date bucketing: a timestamp just before and just after midnight
  Pacific maps to the correct quota day.
- Quota pre-flight estimate matches the `--dry-run` math for a given query set.

### Integration tests (SQLite)
- User-column preservation (the core guarantee): insert a video, set
  `user_notes` and `starred`, run an upsert with changed stats, assert
  `user_notes` and `starred` are unchanged and stats updated.
- `first_seen_at` set on insert and unchanged on re-upsert.
- `last_api_refresh_at` unchanged when no API field changed; updated when one
  field changed.
- Idempotency: running discover twice on identical mock data yields one row per
  video (no duplicates) and an unchanged `videos` table aside from snapshots.
- `stats_snapshots`: one row appended per refresh; no duplicate per run.
- `rankings`: dated rows written for all enabled lanes; PK uniqueness enforced.
- `quota_ledger`: increments within a day and starts a fresh row on a new
  Pacific date.
- Rate-limit guards: with `units_used_today` near the cap, pre-flight refuses a
  discover and logs `quota_preflight_stop`; a per-call guard aborts mid-run and
  logs `quota_guard_stop`.
- DB-locked retry: a simulated lock retries and then succeeds.

- **Phase 1: Schema + config.** New `config.py` keys. New `db.py`: connection
  helper (WAL on), `CREATE TABLE IF NOT EXISTS` for all tables including
  `quota_ledger`, migration guard. Tests: schema creation, config validation.
  STOP.
- **Phase 2: Persist via upsert.** Replace timestamped XLSX with DB upserts
  (videos, channels, stats_snapshots), enforce the user-column contract, add
  `run_log`. Tests: user-column preservation, `first_seen_at` / 
  `last_api_refresh_at` behavior, idempotent re-run, snapshot-per-run guard,
  DB-locked retry. STOP.
- **Phase 3: Three rankings.** Bucket assignment, primary metric
  (`views_to_subs_ratio`) plus captured secondary metrics, top-20 per lane,
  `rankings` writes. Tests: bucket union, ranking selection and ties, empty
  lane. STOP.
- **Phase 4: Run modes + rate limiting.** `--discover` / `--refresh`, no-flag
  default, Pacific-date `quota_ledger`, pre-flight and per-call guards. Update
  `--dry-run`. Tests: Pacific-date bucketing, pre-flight refusal, per-call
  abort, quota estimate parity with dry-run. STOP.
- **Phase 5: XLSX export.** `--export-xlsx` snapshot from the DB. Test: export
  reflects current DB state.

## Out of scope for this script

- The interactive HTML dashboard (separate; reads the DB, including
  `quota_ledger` for the units-today widget).
- Cowork scheduling (separate; wraps `--discover` / `--refresh`).
- A UI for editing `user_notes` / `starred` (interim: DB browser or dashboard).

## Decisions: all resolved

- Lanes: Health, Habit, Overall (Overall = top 20 of the tracked pool). Trending
  dropped.
- Metric: primary sort `views_to_subs_ratio`; all metrics captured in DB and
  dashboard.
- Window: 3 days.
- Metric history: yes (latest on `videos` plus `stats_snapshots`).
- Ranking history: yes (dated `rankings` rows).
- XLSX: on-demand `--export-xlsx` only.
- Quota: daily units tracked in `quota_ledger` (Pacific date), shown on the
  dashboard; two-layer rate limiting keeps runs under 10,000.
- No-flag default: discover if none succeeded today, else refresh.
