# Plan: Date Filter Replacement + Dashboard Tab

## Goal

Replace the global single-date `run-select` control with a toggle-based
Period / Specific Date control on all date-sensitive pages, then add a new
Dashboard tab with four category sub-tabs (Topic mix, Breakout, Format &
craft, Lifecycle placeholder).

---

## Locked decisions

- Period mode is the default; toggle right to enter Specific Date mode.
- Period options: Last 7 days / Last 14 days / Last 30 days / Last 3 months /
  Last 6 months / Last 12 months / All time.
- Specific Date mode shows a dropdown of available run dates (same source as
  the current `run-select`).
- Backend receives `start_date` / `end_date` as YYYY-MM-DD ISO strings. Period-
  to-date translation is entirely JS-side. Backend never sees the word "period".
- Single-run views (Board, Interpretation, Trends distribution/trajectory) use
  `active_run_date` (most recent run date whose `run_date` falls within
  `[start_date, end_date]`). No user-facing note — silent fallback.
- Date-insensitive pages (Extract, Models, Prices, Documentation): hide the
  date filter control while those pages are active.
- Build order: Phase 1 = date filter everywhere; Phase 2 = Dashboard tab.
  Do not start Phase 2 until Phase 1 is reviewed and merged.

---

## Files reviewed

| File | Status |
|---|---|
| `index.html` | Reviewed (uploaded) |
| `db.py` | Reviewed (uploaded) |
| `js/main.js` (and other JS modules) | **NOT uploaded** — CC must locate |
| FastAPI routes file | **NOT uploaded** — CC must locate (see Gate 2B) |
| `css/dashboard.css`, `css/tokens.css` | **NOT uploaded** — CC must locate |

---

## Phase 1 — Date filter control (all pages)

### Gate 1A — HTML structure only (no JS)

**`index.html:51-54`** — Replace the `run-picker` label block:

```html
<!-- REMOVE this block: -->
<label class="run-picker">
  <span class="control-label">Run</span>
  <select id="run-select" aria-label="Run date"></select>
</label>
```

Replace with:

```html
<div class="date-filter-control" id="date-filter-control">
  <span class="date-filter-label" id="dfc-label-period">Period</span>
  <button class="date-filter-toggle" id="dfc-toggle" type="button"
          role="switch" aria-checked="false"
          aria-label="Switch to specific date mode">
    <span class="date-filter-thumb" id="dfc-thumb"></span>
  </button>
  <span class="date-filter-label" id="dfc-label-specific">Specific date</span>
  <select id="dfc-period" aria-label="Time period">
    <!-- Populated by js/date-filter.js from PERIOD_OPTIONS constant -->
  </select>
  <select id="dfc-run" aria-label="Run date" hidden>
    <!-- Populated by js/date-filter.js from fetch_run_dates API response -->
  </select>
</div>
```

**`index.html:36-50` (nav block)** — Add Dashboard link after Interpretation:

```html
<a class="page-nav-link" data-route href="/dashboard"
   id="nav-dashboard" tabindex="-1">Dashboard</a>
```

**`index.html` layout section** — Add Dashboard main mount. Insert after the
`#interpretation` block (currently ends around the `</main>` that closes the
interpretation page), before the `#extract` block:

```html
<main class="dashboard" id="dashboard" aria-live="polite" hidden></main>
```

**CSS (`css/dashboard.css` or `css/tokens.css`)** — Add `.date-filter-control`
layout styles. Toggle thumb animates `left` on `[aria-checked="true"]`.
Use existing CSS custom properties from tokens.css (no new color values).

**Note:** existing pages will not render data between this gate and Gate 1C --
`run-select` is gone but pages are not yet wired to the new event. This is
expected; the dashboard runs locally so a transitional broken state is fine.

**CC verifies before committing:**
- `grep -rn "run-select\|run-picker" index.html` returns zero hits (old markup
  fully removed).
- `grep -n "dfc-toggle\|date-filter-control\|nav-dashboard" index.html` returns
  the new elements at the expected lines.
- No browser console errors on page load (the shell loads even without JS wiring).
- CC reports the grep outputs confirming old IDs gone and new IDs present.

**CC commits:**
```
git add -A
git commit -m "feat(ui): replace run-picker with date-filter toggle control -- HTML + CSS"
```

**Mary validates in the browser:**
Open the dashboard. You should see a toggle labeled "Period / Specific date" and
a dropdown where the Run picker used to be. The Dashboard link should appear in
the top nav. Pages will not show data yet -- that is expected until Gate 1C.

---

### Gate 1B — New `js/date-filter.js` module

New ES module. No global exports. Called once from `main.js` after run dates
are fetched.

**Constants — no magic numbers anywhere in this file:**

```js
// Period definitions — single source of truth.
// 'days' = subtract N days; 'months' = subtract N calendar months;
// 'all' = no start_date bound.
export const PERIOD_OPTIONS = [
  { key: 'p7d',  label: 'Last 7 days',    days: 7      },
  { key: 'p14d', label: 'Last 14 days',   days: 14     },
  { key: 'p30d', label: 'Last 30 days',   days: 30     },
  { key: 'p3m',  label: 'Last 3 months',  months: 3    },
  { key: 'p6m',  label: 'Last 6 months',  months: 6    },
  { key: 'p12m', label: 'Last 12 months', months: 12   },
  { key: 'all',  label: 'All time',       all: true    },
];

export const DEFAULT_PERIOD_KEY = 'p7d';

const IDS = {
  control:       'date-filter-control',
  toggle:        'dfc-toggle',
  thumb:         'dfc-thumb',
  labelPeriod:   'dfc-label-period',
  labelSpecific: 'dfc-label-specific',
  periodSel:     'dfc-period',
  runSel:        'dfc-run',
};

export const EVENT_NAME = 'date-filter-change';
```

**`nowEastern()`** — returns today's YYYY-MM-DD in Eastern time, no library:

```js
function nowEastern() {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York',
  }).format(new Date());
}
```

**`computeStartDate(option)`** — pure, returns a YYYY-MM-DD string or `null`
for "All time":

```js
function computeStartDate(option) {
  if (option.all) return null;
  const d = new Date();                     // UTC instant
  if (option.days) {
    d.setDate(d.getDate() - option.days);
  } else if (option.months) {
    d.setMonth(d.getMonth() - option.months);
  }
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York',
  }).format(d);
}
```

**`buildFilter(state, allRunDates)`** — pure, returns the filter object
dispatched in the event `detail`:

```js
// Returns:
// {
//   mode: 'period' | 'specific',
//   start_date: string | null,   // YYYY-MM-DD; null = no lower bound
//   end_date: string | null,     // YYYY-MM-DD; null = no upper bound
//   active_run_date: string,     // most recent run in window; never null
//   run_count: number,
// }
```

Logic:
- Period mode: compute `start_date` from the selected `PERIOD_OPTIONS` entry.
  `end_date` = `nowEastern()`. Filter `allRunDates` (already DESC from API) to
  those >= `start_date` (or all, if `start_date` is null) and <= `end_date`.
  `active_run_date` = first element of the filtered list (most recent).
  `run_count` = filtered list length.
- Specific mode: `start_date = end_date = active_run_date = state.runDate`.
  `run_count = 1`.
- Edge case: if no run dates fall in the window, `active_run_date` = the
  most recent available run date overall (never undefined). Log a warning.

**`initDateFilter(allRunDates)`** — exported; called from `main.js`:
1. Populate `#dfc-period` `<option>` elements from `PERIOD_OPTIONS`.
2. Populate `#dfc-run` `<option>` elements from `allRunDates`.
3. Initialize state: `mode='period'`, `periodKey=DEFAULT_PERIOD_KEY`.
4. Attach `#dfc-toggle` click handler (flip mode, update aria-checked, toggle
   hidden on `#dfc-period` / `#dfc-run`).
5. Attach `#dfc-period` and `#dfc-run` change handlers.
6. Dispatch initial `EVENT_NAME` event.

**`dispatchFilter(filter)`** — internal:
```js
document.dispatchEvent(new CustomEvent(EVENT_NAME, { detail: filter }));
```

**CC verifies before committing:**
- Open the browser console and run:
  `document.addEventListener('date-filter-change', e => console.log(e.detail))`
  then click the toggle and change period values. Confirm the event fires and
  the logged `detail` object contains `mode`, `start_date`, `end_date`,
  `active_run_date`, and `run_count` with plausible values.
- Confirm "Last 7 days" produces a `start_date` 7 days ago in Eastern time
  (not UTC -- these differ by 4-5 hours).
- Confirm "All time" produces `start_date: null`.
- CC reports all console outputs.

**CC commits:**
```
git add -A
git commit -m "feat(ui): add date-filter.js -- period/specific toggle, Eastern date math, event dispatch"
```

**Mary validates in the browser:**
Toggle between Period and Specific Date. The dropdown should switch from period
options (Last 7 days through All time) to the run date list and back. No page
content renders yet -- that is still expected until Gate 1C.

---

### Gate 1C — Migrate existing pages to consume `date-filter-change`

CC must first locate all `run-select` references in JS:
```
grep -rn "run-select" dashboard/static/js/
```

For each page module found:

**Board** — replace `document.getElementById('run-select').value` with
`e.detail.active_run_date` in the `date-filter-change` listener. The Board
never uses `start_date` / `end_date` (it always shows a single run's leaderboard).

**Trends** — `fetch_rank_history` (db.py:1676) already carries no `run_date`
bound by design; verify no `run_date` argument is passed at the call site and
leave that call unchanged. Replace `run-select` references in the distribution
and trajectory charts with `e.detail.active_run_date`.

**Interpretation** — replace `run-select` reference with
`e.detail.active_run_date`.

**Extract, Models, Prices, Documentation** — hide `#date-filter-control` while
these pages are active. Add a helper in the router (in `main.js` or wherever
page switching lives):
```js
document.getElementById('date-filter-control').hidden = !isDateSensitive(route);
```
where `isDateSensitive` returns `true` for `board`, `trends`, `interpretation`,
`dashboard` and `false` for the rest.

**Initial load** — `main.js` currently fetches run dates to populate `run-select`.
Change that call to pass the result to `initDateFilter(runDates)` instead.
The old `run-select` element is gone; confirm there are no other references.

**CC verifies before committing:**
- `grep -rn "run-select" dashboard/static/js/` returns zero hits.
- Load Board, Trends, Interpretation in the browser; confirm each renders data
  with the default "Last 7 days" period selected. Report pass/fail per page.
- Load Extract, Models, Prices, Documentation; confirm `#date-filter-control`
  is hidden on each. Report pass/fail per page.
- Change period to "Last 30 days"; confirm Board re-fetches. Switch to Specific
  Date mode and pick an older date; confirm Board re-renders for that date.
- CC reports all results before Phase 2 begins.

**CC commits:**
```
git add -A
git commit -m "feat(ui): wire all pages to date-filter-change event -- remove run-select"
```

**Mary validates in the browser:**
- Go to Board. It should load with "Last 7 days" selected showing the current
  leaderboard.
- Switch the period to "Last 30 days". The board should reload.
- Switch to Specific Date mode. Pick an older date. The board should show that
  run's rankings.
- Go to Trends. The bump chart and other charts should load as before.
- Go to Interpretation. Should show the most recent run's summary.
- Go to Extract. The date filter should not be visible in the header.

---

## Phase 2 — Dashboard tab

### Gate 2A — New `db.py` helper functions

Add at the end of db.py (after `fetch_rank_history` at db.py:1676). No edits to
any existing function.

**All three helpers share the same deduplication shape**: a video is included if
it ranked in any run within `[start_date, end_date]`, deduplicated to unique
`video_id`. `start_date` and `end_date` are optional (None = no bound); omit
the bound clause rather than passing NULL.

**Band boundaries constant** (no magic numbers) — mirror the existing
`DURATION_BANDS` pattern at db.py (CC: grep for `DURATION_BANDS` to find it):
```python
RATIO_BANDS: list[tuple[str, float | None, float | None]] = [
    ('<1x',    None, 1.0),
    ('1-10x',  1.0,  10.0),
    ('10-100x',10.0, 100.0),
    ('100x+',  100.0, None),
]
```

---

**`fetch_dashboard_topic_mix(conn, bucket, start_date, end_date)`**

Returns: `{ subniche_counts, lane_counts, category_counts, topic_counts }`

Deduped video set SQL:
```sql
SELECT DISTINCT r.video_id,
       v.matched_queries,
       v.buckets,
       v.category_id,
       v.topic_categories
FROM rankings r
LEFT JOIN videos v ON v.video_id = r.video_id
WHERE r.bucket = :bucket
  [AND r.run_date >= :start_date]   -- omit clause when start_date is None
  [AND r.run_date <= :end_date]     -- omit clause when end_date is None
```

Post-query aggregation (Python, same pattern as `fetch_filter_options`
at db.py:1329):
- `subniche_counts`: explode `matched_queries` on `|`, count by query string,
  attach lane via the `buckets` column (health/habit/overall). Sort by count DESC.
- `lane_counts`: count distinct `video_id` per lane from `buckets` column.
- `category_counts`: count by `category_id`; LEFT JOIN `categories` table to
  add `title`. Sort by count DESC.
- `topic_counts`: explode `topic_categories` on `|`, count by topic string.
  Sort by count DESC.

---

**`fetch_dashboard_breakout(conn, bucket, start_date, end_date)`**

Returns: `{ leaderboard, band_counts, survivorship_note: True }`

Same deduped video set; also LEFT JOIN `channels c ON c.channel_id = v.channel_id`
to get `c.subscriber_count`.

- `leaderboard`: top 20 by `v.views_to_subs_ratio DESC`, excluding rows where
  `v.views_to_subs_ratio IS NULL` or `c.subscriber_count IS NULL`. Include:
  `video_id`, `title`, `channel_title`, `view_count`, `views_to_subs_ratio`,
  `subscriber_count`.
- `band_counts`: bin each video into `RATIO_BANDS` by `views_to_subs_ratio`.
  Return `{ band: str, health: int, habit: int }[]` (count per lane per band).
  A video with a NULL ratio is excluded from band counts (not binned to `<1x`).
- Always include `survivorship_note: True` in the response to signal that the JS
  must display the caveat.

---

**`fetch_dashboard_format(conn, bucket, start_date, end_date)`**

Returns: `{ duration_dist, title_stats, like_comment_pairs, publish_heatmap }`

Same deduped video set; also fetch: `duration_seconds`, `title`, `like_count`,
`comment_count`, `published_at`.

- `duration_dist`: bin `duration_seconds` into existing `DURATION_BANDS`
  (db.py: grep for `DURATION_BANDS`). Return `{ band: str, count: int }[]`.
- `title_stats`: Python over `title` strings. Return:
  `{ avg_length, pct_has_number, pct_has_question, pct_has_emoji }`.
  All percentages as 0-100 floats. "Has number" = any digit in the title.
  "Has emoji" = any character with `ord(c) > 0x1F300` (rough emoji range).
- `like_comment_pairs`: list of `{ like_count, comment_count, ratio }` for
  videos where `comment_count > 0` (zero-comment guard — never divide). Used
  for the scatter chart. Exclude NULLs.
- `publish_heatmap`: for each video where `published_at` is not null or
  unparseable, convert to Eastern with:
  ```python
  dt = _parse_instant(row['published_at'])
  if dt is None: continue
  eastern = dt.astimezone(ZoneInfo('America/New_York'))
  ```
  Return `{ day_of_week: int (0=Mon), hour: int (0-23), count: int }[]`.
  Aggregate over all videos in the deduped set.

**CC verifies before committing:**
- Open a Python shell, connect to the real DB, and call each helper with a known
  date range (e.g., the last 10 run dates). Report:
  - Row count from `fetch_dashboard_topic_mix` (unique video count, top 5
    subniche entries).
  - Row count from `fetch_dashboard_breakout` leaderboard (should be <= 20);
    confirm no row has a NULL `views_to_subs_ratio`.
  - Confirm `fetch_dashboard_format` `like_comment_pairs` contains no rows
    where `comment_count = 0`.
  - Confirm `publish_heatmap` Eastern hours look correct: pick one video's
    `published_at` UTC value, manually convert to Eastern, and confirm the
    heatmap entry matches.
- CC pastes the output summary before proceeding.

**CC commits:**
```
git add -A
git commit -m "feat(db): add fetch_dashboard_topic_mix, fetch_dashboard_breakout, fetch_dashboard_format"
```

**Mary validates:**
CC will paste a short summary: how many unique videos were found for the sample
date range, and a few sample rows from each helper. Review whether the numbers
look roughly right for what you know is in the DB (for example, ~298 unique
videos across the 10 June runs).

---

### Gate 2B — New FastAPI endpoints

**Locate the routes file first:**
```
grep -rn "fetch_run_dates" dashboard/
```
CC must identify the file (likely `routes.py`, `api.py`, or `main.py` in the
dashboard package) and cite its path before writing new endpoints. If the route
handling spans multiple files, flag before proceeding.

**New endpoints** — follow the existing route patterns in that file exactly
(same dependency injection for `conn`, same error shape, same 400 validation):

```
GET /api/dashboard/topic-mix
  ?bucket=health|habit|overall   (required)
  &start_date=YYYY-MM-DD         (optional)
  &end_date=YYYY-MM-DD           (optional)
  → calls fetch_dashboard_topic_mix
  → JSON response shape mirrors the helper's return dict

GET /api/dashboard/breakout
  ?bucket=...  &start_date=...  &end_date=...
  → calls fetch_dashboard_breakout

GET /api/dashboard/format
  ?bucket=...  &start_date=...  &end_date=...
  → calls fetch_dashboard_format
```

**Validation rules** (for all three endpoints):
- `bucket` must be in `{'health', 'habit', 'overall'}` — 400 otherwise.
- `start_date` and `end_date`, when present, must match `r'^\d{4}-\d{2}-\d{2}$'`
  — 400 otherwise.
- Either, both, or neither date param is valid. `end_date` without `start_date`
  is valid (and vice versa).

**No Lifecycle endpoint in this phase** — those charts await the pipeline
snapshot history work described in the handoff.

**CC verifies before committing:**
- Call each endpoint via `curl` or `httpx` and report:
  - Valid params: confirm a 200 response with a data payload (report the top-
    level keys and counts, e.g., "subniche_counts: 12 entries").
  - Invalid `bucket` value (e.g., `bucket=foo`): confirm 400 response.
  - Malformed `start_date` (e.g., `start_date=yesterday`): confirm 400 response.
  - No date params at all: confirm a valid response (All time behavior).
- CC pastes the status codes and response shapes for all test calls.

**CC commits:**
```
git add -A
git commit -m "feat(api): add /api/dashboard/topic-mix, /api/dashboard/breakout, /api/dashboard/format"
```

**Mary validates in the browser:**
In your browser address bar, navigate to
`http://localhost:<port>/api/dashboard/topic-mix?bucket=health` and confirm you
see a JSON response with data (not an error message). Do the same for
`/api/dashboard/breakout?bucket=health` and `/api/dashboard/format?bucket=health`.
You do not need to read the full JSON -- just confirm data is coming back.

---

### Gate 2C — New `js/dashboard.js` module

New ES module. Registered in `main.js` router for the `/dashboard` route.

**Sub-tab structure** — HTML is rendered by this module into `#dashboard`
(replaceChildren pattern, same as other page modules). Four sub-tabs:
`Topic mix`, `Breakout`, `Format & craft`, `Lifecycle`.

```html
<!-- Sub-tab nav rendered inside #dashboard -->
<nav class="lane-tabs dashboard-subtabs" role="tablist" aria-label="Dashboard section">
  <button role="tab" data-section="topic-mix"   aria-selected="true">Topic mix</button>
  <button role="tab" data-section="breakout"                        >Breakout</button>
  <button role="tab" data-section="format"                          >Format & craft</button>
  <button role="tab" data-section="lifecycle"                       >Lifecycle</button>
</nav>
<div id="dashboard-section-panel"></div>
```

**`render(filter, lane)`** — called on `date-filter-change` and on sub-tab
change. Reads current sub-tab from state. Passes `bucket = lane` (the active
lane-tab value), `start_date`, `end_date` from filter to the matching endpoint.

**ECharts initialization** — same `echarts.init(el, null, { renderer: 'canvas' })`
pattern as `trends.js`. Store all chart instances in a module-level array;
call `.resize()` on all of them in a single `window.addEventListener('resize', ...)`
handler.

**Charts per section:**

_Topic mix_
- Sub-niche volume: horizontal bar, `matched_queries` count, lane color per bar.
  (Chart #1 in mockup — replicate that ECharts config exactly.)
- Lane composition: donut. (Chart #7 in mockup.)
- YouTube category: horizontal bar. (Chart #8 in mockup.)
- Topic tags: horizontal bar. (Chart #9 in mockup.)

_Breakout_
- Ratio bands x lane: stacked horizontal bar using `RATIO_BANDS` keys.
  (Chart #5 in mockup.)
- Leaderboard: HTML rows with amber ratio value, same `.board .row` CSS pattern
  from the mockup. (Chart #6 in mockup.)
- Show survivorship caveat as a one-line note under the leaderboard if
  `response.survivorship_note === true`.

_Format & craft_
- Duration distribution: horizontal bar using `DURATION_BANDS` keys (same
  labels as the existing filter rail).
- Title stats: four stat cards (avg length, % with number, % with ?, % with emoji).
- Like-to-comment scatter: x = `like_count`, y = `comment_count`, one point per
  video. Only plot videos with `comment_count > 0` (the guard is already applied
  server-side, but double-check before plotting).
- Publish heatmap: ECharts heatmap, x = hour (0-23), y = day_of_week (0-6,
  Mon-Sun), value = count. Tooltip shows Eastern hour.

_Lifecycle (placeholder)_
- Single panel: heading "Lifecycle" + body text:
  "These charts require daily snapshot history from the pipeline. They will
  populate automatically as runs accumulate."
- No API call, no chart initialization.

**CC verifies before committing:**
- Load the Dashboard tab; confirm no browser console errors.
- Click all four sub-tabs; confirm each renders without errors. Confirm the
  Lifecycle tab shows placeholder text and makes no network calls.
- Switch between Health / Habit / Overall lane tabs with Dashboard open; confirm
  charts re-render.
- Change the period dropdown with Dashboard open; confirm charts re-render.
- Switch to Specific Date mode; confirm charts re-render.
- Resize the browser window; confirm charts resize without layout breakage.
- CC reports any console warnings or errors.

**CC commits:**
```
git add -A
git commit -m "feat(ui): add Dashboard tab -- Topic mix, Breakout, Format & craft, Lifecycle placeholder"
```

**Mary validates in the browser:**
- Click the Dashboard tab. Click through all four sub-tabs. Topic mix, Breakout,
  and Format & craft should show charts. Lifecycle should show a placeholder
  message, no charts.
- Switch between Health, Habit, Overall. The charts should update.
- Change the period. The charts should update.
- Switch to Specific Date mode and pick a date. The charts should update.
- Resize your browser window. The charts should resize cleanly.

---

## Risk flags

1. **Routes file not uploaded** (Gate 2B): CC must locate via grep before writing
   any endpoint code. If the route handling spans more than one file, stop and
   flag.

2. **Eastern date for period boundary** (Gate 1B): JS must compute `start_date`
   in Eastern time, not UTC. `nowEastern()` in the plan above uses
   `Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' })` — this is
   the correct approach; do not use `toISOString().slice(0, 10)` (that is UTC).

3. **`publish_heatmap` Eastern conversion** (Gate 2A): `published_at` is stored
   as a UTC ISO string. Use `_parse_instant()` (db.py: grep for `_parse_instant`)
   to get an aware UTC datetime, then `.astimezone(ZoneInfo('America/New_York'))`.
   Never compare `published_at` lexically for hour/day bucketing.

4. **Zero-comment guard** (Gate 2A): `like_comment_pairs` must exclude videos
   with `comment_count = 0` or NULL. Enforce in the db helper — do not leave this
   to the JS layer.

5. **`views_to_subs_ratio` leaderboard null exclusion** (Gate 2A): exclude rows
   where `views_to_subs_ratio IS NULL` or `subscriber_count IS NULL` from the
   leaderboard. A NULL ratio should not appear as position #1.

6. **No em dashes** anywhere in generated code, comments, or UI strings.
