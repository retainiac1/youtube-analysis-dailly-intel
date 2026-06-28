# Plan: Date Filter Replacement + Dashboard Tab

Implementation plan for Claude Code. Plan-before-execute with STOP gates. Each
gate ends with three blocks: **CC verifies** (technical checks CC runs and
reports), **CC commits** (the git command), **Mary validates** (what Mary checks
in the browser). Do not cross a STOP gate until Mary signs off.

No em dashes anywhere in code, comments, commit messages, or UI strings.

---

## Goal

Replace the global single-date `run-select` control with a toggle-based
Period / Specific Date filter on every date-sensitive page, then add a new
lane-scoped Dashboard tab with four category sub-tabs (Topic mix, Breakout,
Format & craft, Lifecycle placeholder).

---

## Locked decisions

- **Two modes, one toggle.** Period mode (default) shows a relative-window
  dropdown. Specific Date mode shows a run-date dropdown. Toggle right enters
  Specific Date.
- **Period dropdown options, in order:** Latest run (default) / Last 7 days /
  Last 14 days / Last 30 days / Last 3 months / Last 6 months / Last 12 months /
  All time.
- **Default on first open is "Latest run"** which resolves to a single date:
  the most recent run_date. So every page opens on the most recent run. "Latest
  run" auto-follows as new runs land; Specific Date pins a chosen date.
- **The filter is global and persists across pages.** One control in the sticky
  header; its state survives navigation. Opening any date-sensitive page applies
  the current filter immediately (see "Persistence contract" below). It is NOT
  per-page and does NOT reset on navigation.
- **Backend contract:** JS translates the selection into `start_date` /
  `end_date` (YYYY-MM-DD, Eastern) before calling the API. The backend never
  sees the word "period." Bound semantics:
  - Latest run: `start_date = end_date = <most recent run_date>`
  - Specific date: `start_date = end_date = <picked date>`
  - Last N days/months: `start_date = <computed, Eastern>`, `end_date = <today, Eastern>`
  - All time: `start_date = null`, `end_date = null` (no bounds)
- **First-open default is a single run: the latest run_date.** "Default to
  latest run" means the INITIAL state when a fresh visitor opens the site lands
  on the most recent run (one run), so something sensible loads. It does NOT mean
  single-run pages should permanently ignore the filter. See "Deferred: make all
  pages period-aware" below; that is the originally intended end state, not a
  later add-on.
- **Single-run views in Phase 1 only** (Board, Interpretation, Trends view-count
  distribution): for Phase 1 these resolve to `active_run_date` = the most recent
  run_date within the window. This is an INTERIM implementation limit, not a
  product decision. Phase 1 wires the control and the multi-run Trends charts;
  making Board / Interpretation / histogram respond to the window is the deferred
  work below.
- **Trend charts** (Trends bump chart, Trends trajectory) window to the
  selection. When the window contains fewer than two runs, the bump chart shows
  an empty state ("Select a wider period to see rank movement") rather than a
  degenerate single-column chart.
- **The Dashboard is lane-scoped** by the existing Health / Habit / Overall lane
  tabs, exactly like Trends. There is no lane-composition donut (it would be a
  single slice under lane scoping).
- **Date-insensitive pages** (Extract, Models, Prices, Documentation) hide the
  filter control while active.
- **Build order:** Phase 1 = date filter everywhere. Phase 2 = Dashboard tab.
  Do not start Phase 2 until Phase 1 is reviewed and merged.

---

## Deferred: make all pages period-aware (its own phase)

**Status: required, not yet built. This is the originally intended behavior, not
a new request.**

Framing, so the history is not lost: "default to latest run" was always meant as
the FIRST-OPEN state only (land on the most recent run so something loads). It
was mis-scoped during Phase 1 into a permanent rule where Board, Interpretation,
and the view-count histogram always show the latest run and never respond to the
filter. There was no product decision to make those pages ignore the window; the
single-run resolution is an implementation artifact of Phase 1. This phase
corrects that.

**The requirement:** after first open, whatever the user selects (a period
window or a specific date) filters the underlying data, and that filtered or
aggregated data drives Board rows, Interpretation, and the histogram, not just
the Trends charts. First-open default stays exactly as is: the single latest
run_date.

**Why it is its own phase, not folded into Phase 2:** the Dashboard tab (Phase 2)
can ship without this, and this is the same complexity class as the multi-select
dates that were deferred. It is a real shift from single-run views to
windowed-aggregate views. Implementation can reuse Phase 2's
`fetch_dashboard_*` dedup-across-window helpers, but the three pages do not
aggregate the same way, and each needs a decision before building:

- **Histogram (view-count distribution across the window):** dedup videos that
  appear in any selected run by `video_id`, then bucket. Open question: WHICH
  view_count per video, since a video in three runs has three historical counts.
  Current run snapshot? The count from its most recent selected run? This is the
  same view_count-semantics question that deferred the multi-select distribution
  piece; it is still unanswered.
- **Board (a ranked leaderboard):** "aggregate a ranking across N runs" requires
  a rule. Best rank achieved in the window? Most recent rank? Mean rank? This is
  a product decision, not just a dedup.
- **Interpretation (per-run generated LLM text):** "period-aware" could mean
  re-synthesizing across the selected runs (real prompt work and real classify
  cost) or simply showing the most recent run's stored summary. Very different
  efforts; pick deliberately.

Do not start building this phase until those three rules are decided. Recording
the requirement now so it persists; the decisions happen when the phase is
scoped.

---

## Persistence contract (read before Phase 1)

Two update paths keep the filter global and consistent:

1. **Change path.** When the user changes the toggle or a dropdown,
   `date-filter.js` recomputes the filter and dispatches a
   `date-filter-change` CustomEvent on `document`. Page modules listen and
   re-render, but ONLY when their page is currently visible (guard on the page
   element not being `hidden`), so a background page does no work.

2. **Activation path.** When the router shows a date-sensitive page,
   it calls that page's render with the CURRENT filter, read synchronously from
   `date-filter.js` via the exported `getCurrentFilter()`. This is what makes
   the selection persist across navigation: a page that was hidden when the
   filter last changed still renders with the live selection when reopened.

`date-filter.js` owns the single source of truth for the current filter
(module-level state). It exposes `getCurrentFilter()` (pure read) and
`initDateFilter(allRunDates)` (one-time setup). No other module stores filter
state.

---

## Gate 0 — Repository discovery (no code changes)

The uploaded `db.py` was schema v11; the live DB has since migrated to v12, so
**line numbers in this plan are approximate**. Ground every citation in the live
code by symbol name, not line number.

CC performs and reports, before any edits:

- Print the repo tree for the dashboard package (depth 3). Identify and cite the
  real paths for: the static JS directory, the FastAPI routes file(s), `db.py`,
  and the CSS files (`tokens.css`, `dashboard.css` or equivalent).
- Locate the page router / page-switching logic (where pages are shown and
  hidden). Cite file and the function or block that handles navigation. This is
  where the activation path (above) hooks in.
- Confirm the live `db.py` schema version: `PRAGMA user_version`. Report it.
- Re-locate by symbol and report current line numbers for: `fetch_run_dates`,
  `fetch_lane`, `fetch_rank_history`, `fetch_filter_options`, `_parse_instant`,
  `DURATION_BANDS`.
- Confirm the existing API path style (hyphens vs underscores, `/api` prefix)
  from one or two existing routes, so the new endpoints match house convention.
- Report how existing page modules subscribe to the run picker today (so the new
  event wiring matches the existing pattern).

Use `grep -rE` (extended regex) for all searches. On macOS BSD grep, `\|` is
NOT alternation; always use `-E "a|b"`.

**STOP** — Mary reviews the discovered paths and the schema version before any
changes. CC should surface anything surprising (e.g. routes split across
multiple files, an unexpected schema version).

---

## Phase 1 — Date filter control (all date-sensitive pages)

**Status: SHIPPED (as-built).** Gates 1A-1C are implemented and committed on
`feat/date-filter`. The gate text below has been updated to match the shipped code;
it diverged from the original plan in three ways: (1) the mode control is a
two-segment PILL control, not a switch; (2) the date filter sits on its own
second-tier row so the theme toggle stays on the top nav row; (3) the change path is
one centralized listener in `main.js`, not a listener per page module. Single-run
behavior of Board / Interpretation / histogram is the interim limit corrected by the
"Deferred: make all pages period-aware" phase above.

### Gate 1A — HTML + CSS only (no JS, no Dashboard)

The Dashboard nav link and `#dashboard` mount are intentionally NOT added here.
They land in Gate 2C when the route is actually wired, so Phase 1 never ships a
dead link.

**Replace the run-picker block** (uploaded `index.html` around lines 51-54;
confirm current location). Remove:

```html
<label class="run-picker">
  <span class="control-label">Run</span>
  <select id="run-select" aria-label="Run date"></select>
</label>
```

Replace with (as-built: a two-segment PILL control, mirroring the lane tabs, not a
switch):

```html
<div class="date-filter-control" id="date-filter-control">
  <div class="date-mode-tabs" role="tablist" aria-label="Date filter mode">
    <button class="date-mode-tab" type="button" role="tab"
            id="dfc-mode-period" data-mode="period"
            aria-selected="true" tabindex="0">Period</button>
    <button class="date-mode-tab" type="button" role="tab"
            id="dfc-mode-specific" data-mode="specific"
            aria-selected="false" tabindex="-1">Specific date</button>
  </div>
  <select id="dfc-period" aria-label="Time period">
    <!-- Populated by js/date-filter.js from PERIOD_OPTIONS -->
  </select>
  <select id="dfc-run" aria-label="Run date" hidden>
    <!-- Populated by js/date-filter.js from the run-dates API response -->
  </select>
</div>
```

**Placement (as-built):** `#date-filter-control` is its OWN second-tier row, a direct
child of `.sticky-header` (after `.topbar`, before `.lane-row`). The `#theme-toggle`
stays alone in `.topbar-controls`, so it renders on the top nav row, far right,
identical to the date-insensitive pages. The old run-picker label is removed.

**CSS** (in the CSS file Gate 0 identified). Add `.date-filter-control` layout, plus
`.date-mode-tabs` / `.date-mode-tab` segmented-pill styles (mirroring `.lane-tabs` /
`.lane-tab`, with the active pill using the NEUTRAL page-nav fill, not a lane colour).
The active pill is driven by `aria-selected="true"`. Keep `.date-filter-control[hidden]
{ display: none; }` (the control is `inline-flex`) and
`.sticky-header > #date-filter-control { align-self: flex-start; }`. Use existing CSS
custom properties from the tokens file; introduce no new raw color values.

**Transitional state:** between this gate and Gate 1C, pages will not render
data (the old `run-select` is gone, new wiring is not in yet). This is expected;
the app runs locally.

**CC verifies before committing:**
- `grep -rEn "run-select|run-picker" index.html` returns zero hits.
- `grep -En "date-mode-tab|date-filter-control" index.html` shows the new markup.
- Page shell loads with no console errors (no JS wiring yet, so the control is
  inert; that is fine).
- Report the grep outputs.

**CC commits:**
```
git add -A
git commit -m "feat(ui): replace run-picker with date-filter toggle control (HTML + CSS)"
```

**Mary validates in the browser:**
Open the app. Where the Run picker used to be, you should see a "Period /
Specific date" toggle and a dropdown. The toggle will not do anything yet, and
pages will not show data yet. That is expected.

---

### Gate 1B — New `js/date-filter.js` + minimal wiring

New ES module, plus the one `main.js` call that makes it run, so the control is
testable standalone. Pages do not consume the event yet (that is Gate 1C); the
event simply fires into the void until then.

**Constants (no magic numbers in this file):**

```js
// Single source of truth for the period dropdown.
// 'latest'  -> start = end = most recent run_date (default)
// 'days'    -> subtract N days
// 'months'  -> subtract N months
// 'all'     -> no bounds
export const PERIOD_OPTIONS = [
  { key: 'latest', label: 'Latest run',   latest: true },
  { key: 'p7d',    label: 'Last 7 days',  days: 7      },
  { key: 'p14d',   label: 'Last 14 days', days: 14     },
  { key: 'p30d',   label: 'Last 30 days', days: 30     },
  { key: 'p3m',    label: 'Last 3 months', months: 3   },
  { key: 'p6m',    label: 'Last 6 months', months: 6   },
  { key: 'p12m',   label: 'Last 12 months', months: 12 },
  { key: 'all',    label: 'All time',     all: true    },
];

export const DEFAULT_PERIOD_KEY = 'latest';
export const EVENT_NAME = 'date-filter-change';

const IDS = {
  control: 'date-filter-control',
  modePeriod: 'dfc-mode-period', modeSpecific: 'dfc-mode-specific',
  periodSel: 'dfc-period', runSel: 'dfc-run',
};
```

**`nowEastern()`** returns today's YYYY-MM-DD in Eastern, no library:

```js
function nowEastern() {
  return new Intl.DateTimeFormat('en-CA',
    { timeZone: 'America/New_York' }).format(new Date());
}
```

**`computeStartDate(option)`** returns YYYY-MM-DD or null. The month branch
clamps the day to avoid JS month-overflow (e.g. May 31 minus 3 months must not
roll forward into March):

```js
function computeStartDate(option) {
  if (option.all || option.latest) return null; // 'latest' resolved elsewhere
  const d = new Date();
  if (option.days) {
    d.setDate(d.getDate() - option.days);
  } else if (option.months) {
    const day = d.getDate();
    d.setDate(1);                       // clamp first to avoid overflow
    d.setMonth(d.getMonth() - option.months);
    const last = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate();
    d.setDate(Math.min(day, last));     // restore day, clamped to month length
  }
  return new Intl.DateTimeFormat('en-CA',
    { timeZone: 'America/New_York' }).format(d);
}
```

**`buildFilter(state, allRunDates)`** is pure. `allRunDates` is the DESC list
from the run-dates API. Returns:

```js
// {
//   mode: 'period' | 'specific',
//   start_date: string | null,   // YYYY-MM-DD; null = no lower bound
//   end_date: string | null,     // YYYY-MM-DD; null = no upper bound
//   active_run_date: string | null,  // most recent run in window
//   run_count: number,
// }
```

Logic:
- **Empty `allRunDates`:** return all-null dates, `active_run_date = null`,
  `run_count = 0`. Callers must tolerate a null active date (defensive; Mary's
  DB has data, but a fresh DB must not throw).
- **Specific mode:** `start = end = active_run_date = state.runDate`,
  `run_count = 1`.
- **Period, 'latest':** `start = end = active_run_date = allRunDates[0]`,
  `run_count = 1`.
- **Period, 'all':** `start = end = null`. `active_run_date = allRunDates[0]`.
  `run_count = allRunDates.length`.
- **Period, days/months:** `start = computeStartDate(option)`,
  `end = nowEastern()`. Filter `allRunDates` to `start <= d <= end` (lexical
  compare is correct: these are zero-padded ISO date keys). `active_run_date` =
  first of the filtered list, or `allRunDates[0]` if the window caught nothing
  (defensive). `run_count` = filtered length.

**`initDateFilter(allRunDates)`** (exported, called once from `main.js`):
1. Populate `#dfc-period` from `PERIOD_OPTIONS`; select `DEFAULT_PERIOD_KEY`.
2. Populate `#dfc-run` from `allRunDates`; default-select index 0 (most recent).
3. State: `{ mode: 'period', periodKey: 'latest', runDate: allRunDates[0] ?? null }`.
4. Mode tabs (as-built): each `.date-mode-tab` click sets the mode; `applyModeUI`
   sets `aria-selected` + roving `tabIndex` on the two tabs and shows/hides
   `#dfc-period` vs `#dfc-run`; then recompute + dispatch. ArrowLeft/Right rove and
   select between the two tabs (matches the lane tablist).
5. Change handlers on both dropdowns: update state, recompute, dispatch.
6. Compute the initial filter and store it, but see the ordering note below
   before dispatching.

**`getCurrentFilter()`** (exported, pure read): returns the last computed filter
object. The activation path uses this.

**Ordering (prevents a lost initial event):** `main.js` must register page
listeners BEFORE calling `initDateFilter`, OR pages must rely solely on the
activation path for first render. Simplest correct approach: `initDateFilter`
computes and stores the initial filter but does NOT need to dispatch on load,
because the router's activation path renders the first visible page from
`getCurrentFilter()`. Dispatch only on subsequent user changes. CC: confirm
which page is shown on load and that its activation render reads
`getCurrentFilter()`.

**Wiring:** in `main.js`, the existing call that fetched run dates for
`run-select` now passes them to `initDateFilter(runDates)`. Remove the old
`run-select` population.

**CC verifies before committing:**
- In the console: `JSON.stringify(window.__df ?? 'n/a')` is not the test; instead
  add a temporary listener
  `document.addEventListener('date-filter-change', e => console.log(e.detail))`,
  then toggle and change dropdowns. Confirm `detail` has `mode`, `start_date`,
  `end_date`, `active_run_date`, `run_count`.
- "Latest run" yields `start_date === end_date === <newest run_date>`,
  `run_count === 1`.
- "Last 7 days" yields a `start_date` 7 days back in Eastern (not UTC).
- "Last 3 months" from a 31st does not roll forward (spot check the clamp).
- "All time" yields `start_date === null && end_date === null`.
- Report all console outputs.

**CC commits:**
```
git add -A
git commit -m "feat(ui): add date-filter.js (latest/period/specific toggle, Eastern date math)"
```

**Mary validates in the browser:**
Toggle between Period and Specific Date. The dropdown should swap between the
period list (Latest run through All time) and the run-date list. The default
should read "Latest run." No page data renders yet; that is still expected.

---

### Gate 1C — Wire pages to the filter (consume + activate)

**As-built deviations from the text below:** the change path is ONE centralized
`date-filter-change` listener in `main.js` (not a listener added to each page
module), because every page already reads the shared `state` object and the listener
re-renders only the current route. `enterBoard` gained an activation reload (Board
did not refresh on entry before; this is the one navigation-behavior change). The
bump-chart windowing shipped via optional `start_date`/`end_date` params on
`fetch_rank_history` + `/api/rank-history`. `run-select` is fully removed. The
per-page consume/activate intent below still holds; only the wiring location differs.

CC first locates run-picker references with `grep -rEn "run-select" <js-dir>`
(path from Gate 0).

**Each page module** gains a `date-filter-change` listener that re-renders ONLY
when its page is visible (guard on `!pageEl.hidden`), AND is rendered via the
activation path on navigation using `getCurrentFilter()`.

- **Board** (single-run): render the leaderboard for `filter.active_run_date`.
  If `active_run_date` is null, show the existing empty state. Board ignores
  `start_date` / `end_date`.
- **Interpretation** (single-run): render for `filter.active_run_date`.
- **Trends:**
  - View-count distribution (single-run): use `active_run_date`.
  - Bump chart (trend, now windowed): pass `start_date` / `end_date` to
    `fetch_rank_history` (see the db.py edit below). If `run_count < 2`, render
    the empty state "Select a wider period to see rank movement" and skip the
    chart.
  - Trajectory (trend): window to `start_date` / `end_date`.
- **Extract, Models, Prices, Documentation:** the router hides
  `#date-filter-control` when these are active and shows it otherwise. Add an
  `isDateSensitive(route)` helper returning true for board, trends,
  interpretation (and dashboard in Phase 2).

**db.py edit (backward-compatible):** add optional `start_date` and `end_date`
params to `fetch_rank_history` (currently bucket + optional video_ids). When
provided, add `AND r.run_date >= :start_date` and `AND r.run_date <= :end_date`
to the WHERE. Default `None` preserves the existing all-runs behavior for any
other caller. This reverses the function's prior "no run_date bound" stance ONLY
when bounds are passed; the docstring must be updated to say so. run_date is a
date key, so the comparison is lexical by design (do NOT convert to offset-aware
instants here; that rule applies to `published_at`, not to `run_date`).

**Initial load:** `main.js` passes fetched run dates to `initDateFilter`, then
the router renders the first visible page from `getCurrentFilter()`.

**CC verifies before committing:**
- `grep -rEn "run-select" <js-dir>` returns zero hits.
- Board, Trends, Interpretation render on load with "Latest run" (most recent
  run). Report pass/fail each.
- Extract, Models, Prices, Documentation hide the control. Report pass/fail each.
- Navigate Board -> set "Last 30 days" -> go to Extract -> return to Board:
  Board still shows the Last-30-days selection (persistence). Report result.
- Trends with "Latest run" shows the bump empty state; widening to "Last 14
  days" renders the bump chart. Report result.
- Report the updated `fetch_rank_history` signature and a quick call proving the
  bound works and that an unbounded call still returns all runs.

**CC commits:**
```
git add -A
git commit -m "feat(ui): wire pages to date-filter (consume + activate); window bump chart"
```

**Mary validates in the browser:**
- Board opens on the most recent run.
- Change to "Last 30 days," go to Trends, come back to Board: the selection
  stuck (still Last 30 days). This is the persistence check.
- Trends on "Latest run" shows a "select a wider period" message for the bump
  chart; pick "Last 14 days" and the bump chart appears.
- Interpretation shows the most recent run's summary.
- Extract hides the date control.

**STOP — Phase 1 complete. Do not begin Phase 2 until Mary signs off.**

---

## Phase 2 — Dashboard tab (lane-scoped)

No schema change anywhere in Phase 2. The new db helpers are read-only
functions. Do NOT bump `SCHEMA_VERSION`, do NOT call `init_db`, do NOT add a
migration, and do NOT touch the backup gate. If anything seems to require a
schema change, STOP and flag it.

### Gate 2A — New read-only `db.py` helpers

Append after `fetch_rank_history`. No edits to existing functions. All three
helpers are lane-scoped (take `bucket`) and share one deduplication shape: a
video is included if it ranked in the given `bucket` in any run within
`[start_date, end_date]`, deduplicated to unique `video_id`.

**Dedup pattern (use this exact shape, not multi-column DISTINCT):**

```sql
SELECT v.<cols...>
FROM videos v
WHERE v.video_id IN (
  SELECT DISTINCT r.video_id
  FROM rankings r
  WHERE r.bucket = :bucket
    -- add ONLY when the bound is not None:
    -- AND r.run_date >= :start_date
    -- AND r.run_date <= :end_date
)
```

run_date is a zero-padded date key; the bound comparison is lexical by design
(NOT an offset-aware instant compare; that rule is only for `published_at`).

**Ratio bands constant (no magic numbers), mirroring `DURATION_BANDS`:**

```python
RATIO_BANDS: list[tuple[str, float | None, float | None]] = [
    ('<1x',     None, 1.0),
    ('1-10x',   1.0,  10.0),
    ('10-100x', 10.0, 100.0),
    ('100x+',   100.0, None),
]
```

**`fetch_dashboard_topic_mix(conn, bucket, start_date, end_date)`**
Returns `{ subniche_counts, category_counts, topic_counts }` (no lane donut).
Select `matched_queries, category_id, topic_categories` for the deduped set.
- `subniche_counts`: split `matched_queries` on `|`, count per query, sort DESC.
- `category_counts`: count per `category_id`, then LEFT JOIN the `categories`
  table to attach `title` (do this as a second query keyed by the distinct
  category_ids, or a join in the same statement). Sort DESC. Rows with a NULL
  category_id bucket under a literal label like "Uncategorized".
- `topic_counts`: split `topic_categories` on `|`, count per topic, sort DESC.

**`fetch_dashboard_breakout(conn, bucket, start_date, end_date)`**
Returns `{ leaderboard, band_counts, survivorship_note: True }`. Join
`channels c ON c.channel_id = v.channel_id` for `subscriber_count`.
- `leaderboard`: top 20 by `views_to_subs_ratio DESC`, EXCLUDING rows where
  `views_to_subs_ratio IS NULL` or `subscriber_count IS NULL`. Fields:
  `video_id, title, channel_title, view_count, views_to_subs_ratio,
  subscriber_count`.
- `band_counts`: bin each deduped video into `RATIO_BANDS` by
  `views_to_subs_ratio`. Return `[{ band: str, count: int }]` for the selected
  lane (single lane, so no per-lane split). Videos with a NULL ratio are
  excluded from binning, not dropped into `<1x`.
- `survivorship_note: True` so the JS shows the "best among caught" caveat.

**`fetch_dashboard_format(conn, bucket, start_date, end_date)`**
Returns `{ duration_dist, title_stats, like_comment_pairs, publish_heatmap }`.
Select `duration_seconds, title, like_count, comment_count, published_at`.
- `duration_dist`: bin `duration_seconds` into `DURATION_BANDS`. Return
  `[{ band: str, count: int }]`.
- `title_stats`: over non-null titles, return
  `{ avg_length, pct_has_number, pct_has_question, pct_has_emoji }` (percentages
  0-100). "Has number" = contains any digit. "Has emoji" = any codepoint in a
  rough emoji range; this is an approximation and may miss flags / ZWJ
  sequences, which is acceptable for a content stat.
- `like_comment_pairs`: `[{ like_count, comment_count, ratio }]` ONLY for videos
  with `comment_count > 0` (zero-comment guard enforced here, server-side, never
  in JS). Exclude NULL like/comment.
- `publish_heatmap`: for each video with a parseable `published_at`, convert UTC
  to Eastern, then bucket by weekday and hour:
  ```python
  dt = _parse_instant(row['published_at'])
  if dt is None:
      continue
  eastern = dt.astimezone(ZoneInfo('America/New_York'))
  # day_of_week: 0 = Monday ... 6 = Sunday; hour: 0-23 Eastern
  ```
  Return `[{ day_of_week: int, hour: int, count: int }]`. Never bucket by the
  raw UTC string.

**CC verifies before committing:**
- In a Python shell against the live DB, call each helper for `bucket='health'`
  with (a) Latest-run bounds (start = end = newest run_date) and (b) no bounds.
  Report: deduped video counts, top 5 subniche entries, leaderboard length
  (<= 20 and no NULL ratios), confirmation that `like_comment_pairs` has no
  `comment_count == 0` rows, and a spot-check that one video's
  `publish_heatmap` weekday/hour matches a hand conversion of its
  `published_at` to Eastern.
- Confirm `PRAGMA user_version` is unchanged (no accidental migration).
- Paste the summary.

**CC commits:**
```
git add -A
git commit -m "feat(db): add lane-scoped dashboard read helpers (topic-mix, breakout, format)"
```

**Mary validates:**
CC pastes a short summary. Eyeball whether the deduped video counts look right
for the lane and window (a single run is roughly one lane's daily catch), and
whether the top sub-niches match what you would expect.

---

### Gate 2B — New FastAPI endpoints

Use the routes file and path convention from Gate 0. Match the existing routes'
connection dependency, error shape, and validation style exactly.

```
GET /api/dashboard/topic-mix?bucket=health|habit|overall[&start_date=YYYY-MM-DD][&end_date=YYYY-MM-DD]
GET /api/dashboard/breakout?bucket=...[&start_date=...][&end_date=...]
GET /api/dashboard/format?bucket=...[&start_date=...][&end_date=...]
```
(Adjust hyphen/underscore and `/api` prefix to match house convention from
Gate 0.)

**Validation (all three):**
- `bucket` required, in `{health, habit, overall}`, else 400.
- `start_date` / `end_date`, when present, match `^\d{4}-\d{2}-\d{2}$`, else 400.
- Any combination of present/absent dates is valid (none = All time).

No Lifecycle endpoint this phase (it depends on the pipeline snapshot-history
work, which is a separate plan).

**CC verifies before committing:**
- If the server does not auto-reload, restart it first; note which.
- Curl each endpoint and report status + top-level keys/counts for: valid params;
  invalid `bucket` (expect 400); malformed `start_date` (expect 400); no date
  params (expect 200, All time).
- Paste the results.

**CC commits:**
```
git add -A
git commit -m "feat(api): add dashboard topic-mix, breakout, format endpoints"
```

**Mary validates in the browser:**
Visit `http://localhost:<port>/api/dashboard/topic-mix?bucket=health` (adjust to
the real path) and confirm JSON with data, not an error. Repeat for breakout and
format. You do not need to read the JSON, just confirm data comes back.

---

### Gate 2C — New `js/dashboard.js` + nav link + route

This gate adds everything deferred from Gate 1A.

**HTML (`index.html`):**
- Add the nav link after Interpretation:
  ```html
  <a class="page-nav-link" data-route href="/dashboard"
     id="nav-dashboard" tabindex="-1">Dashboard</a>
  ```
- Add the mount after the `#interpretation` block, before `#extract`:
  ```html
  <main class="dashboard" id="dashboard" aria-live="polite" hidden></main>
  ```

**Router (`main.js`):** register `/dashboard` (match the existing route
registration pattern from Gate 0). Add `dashboard` to `isDateSensitive` so the
filter control shows and the lane tabs apply, like Trends.

**`js/dashboard.js`:** renders into `#dashboard` once with a stable structure,
then updates only the section panel on changes (so the sub-tab nav is never
wiped and its handlers never die):

```html
<nav class="lane-tabs dashboard-subtabs" role="tablist" aria-label="Dashboard section">
  <button role="tab" data-section="topic-mix" aria-selected="true">Topic mix</button>
  <button role="tab" data-section="breakout">Breakout</button>
  <button role="tab" data-section="format">Format &amp; craft</button>
  <button role="tab" data-section="lifecycle">Lifecycle</button>
</nav>
<div id="dashboard-section-panel"></div>
```

- The nav is rendered ONCE on first activation. Re-renders (filter change, lane
  change, sub-tab change) replace only `#dashboard-section-panel`, never the nav.
- `render(filter, lane)` runs on: the `date-filter-change` event (only when
  `#dashboard` is visible), the activation path (via `getCurrentFilter()`), lane
  tab change, and sub-tab change. It calls the endpoint for the active sub-tab
  with `bucket = lane`, `start_date`, `end_date` from `filter`.
- ECharts: init with the same options as the existing Trends charts (renderer
  and theme). Hold instances in a module array; one shared `resize` handler
  calls `.resize()` on all. Dispose instances before re-rendering a section to
  avoid leaks.

**Charts per section (specs are inline; CC does NOT need the mockup file).**
Match the existing app's chart styling (axis, grid, tooltip, lane accent color
read from the active lane). All bar charts are horizontal with category labels
on the y-axis, value on the x-axis, sorted descending.

_Topic mix_
- Sub-niche volume: horizontal bar from `subniche_counts` (label = sub-niche,
  value = count), single accent = the active lane's color.
- YouTube category: horizontal bar from `category_counts` (label = category
  title, value = count).
- Topic tags: horizontal bar from `topic_counts` (label = topic, value = count).

_Breakout_
- Ratio bands: horizontal bar from `band_counts` (label = band in `RATIO_BANDS`
  order, value = count).
- Leaderboard: HTML rows in the existing `.board .row` style, ratio value in the
  amber accent. Columns: rank, title, channel, views, ratio, subscriber_count.
- Caveat line under the leaderboard when `survivorship_note === true`:
  "Best among caught, not best on YouTube. Conditioned on clearing the capture
  view bar."

_Format & craft_
- Duration distribution: horizontal bar from `duration_dist`.
- Title anatomy: four stat cards from `title_stats` (avg length, % with a
  number, % with a question mark, % with an emoji).
- Like-to-comment scatter: x = `like_count`, y = `comment_count`, one point per
  pair in `like_comment_pairs`.
- Publish heatmap: ECharts heatmap, x = hour 0-23, y = weekday 0-6 (Mon-Sun),
  value = count, from `publish_heatmap`. Tooltip states the hour is Eastern.

_Lifecycle (placeholder)_
- One panel: heading "Lifecycle" and body "These charts require daily snapshot
  history from the pipeline. They will populate as runs accumulate." No API
  call, no chart.

**CC verifies before committing:**
- Dashboard tab loads with no console errors. All four sub-tabs render; Lifecycle
  shows the placeholder and makes no network call (confirm in the Network tab).
- Switching Health/Habit/Overall re-renders the charts (lane scoping works).
- Changing the period re-renders; switching to Specific Date re-renders.
- Navigating away and back preserves the filter (activation path) and shows the
  current selection.
- Resizing the window resizes charts without layout breakage.
- Report any warnings/errors and the Network-tab confirmation for Lifecycle.

**CC commits:**
```
git add -A
git commit -m "feat(ui): add lane-scoped Dashboard tab (topic-mix, breakout, format, lifecycle placeholder)"
```

**Mary validates in the browser:**
- Click Dashboard. Click all four sub-tabs. Topic mix, Breakout, Format & craft
  show charts; Lifecycle shows a placeholder.
- Switch Health/Habit/Overall: charts update.
- Change the period and toggle to Specific Date: charts update.
- Leave Dashboard and return: your selection is still there.
- Resize the window: charts resize cleanly.

**STOP — Phase 2 complete.**

---

## Risk flags

1. **Stale line numbers / schema (Gate 0).** Uploaded `db.py` is v11; live is
   v12. Ground citations by symbol, re-confirm lines, report the version.
2. **No schema change in Phase 2.** Read-only helpers only. No version bump, no
   `init_db`, no migration, no backup gate. CC recently triggered an accidental
   migration via `init_db`; do not repeat. Flag if anything seems to need one.
3. **Eastern, not UTC (Gate 1B).** Period boundaries via
   `Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' })`. Never
   `toISOString().slice(0,10)`.
4. **Month overflow (Gate 1B).** Use the day-clamp in `computeStartDate`, or
   "Last 3 months" from a 31st rolls forward incorrectly.
5. **run_date is a date key, compared lexically by design (Gates 1C, 2A).** Do
   NOT "fix" `r.run_date >= :start_date` into an offset-aware instant compare.
   That rule applies to `published_at` (a timestamp), not `run_date`.
6. **Publish heatmap Eastern conversion (Gate 2A).** `_parse_instant` then
   `.astimezone(ZoneInfo('America/New_York'))`. Never bucket the raw UTC string.
7. **Zero-comment guard (Gate 2A).** `like_comment_pairs` excludes
   `comment_count == 0` / NULL, enforced server-side.
8. **Leaderboard NULL exclusion (Gate 2A).** Drop rows with NULL
   `views_to_subs_ratio` or NULL `subscriber_count` so a NULL never ranks #1.
9. **Sub-tab nav must survive re-render (Gate 2C).** Render the nav once;
   replace only `#dashboard-section-panel`. Otherwise click handlers die.
10. **Persistence depends on the activation path (Gates 1B, 1C).** A page reads
    `getCurrentFilter()` on activation; the change event only updates visible
    pages. Missing either path breaks cross-page persistence.
11. **macOS grep.** Use `grep -rE`; `\|` is not alternation in BSD grep.
12. **No em dashes** in code, comments, commits, or UI strings.