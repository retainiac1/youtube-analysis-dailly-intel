# plan-bar-drilldown.md

Architect scope for the bar-to-video-list drill-down. This is the governing
spec, not an implementation plan. CC writes a per-gate implementation plan
(file:line, STOP gates, verify steps) and gets it reviewed BEFORE coding each
gate. Nothing here is a license to code.

## Intent

Clicking a bar in a count chart on the Dashboard page opens a right-anchored
overlay drawer listing the videos behind that bar, rendered in the same row
format as the Board page.

## Decisions locked (do not reopen)

1. UI pattern: right-anchored overlay drawer. Backdrop, no page reflow. The grid
   underneath does not resize, so no ECharts redraw fires on open or close.
2. Fetch: lazy. One endpoint returns the rows on click. Bars do NOT carry their
   videos in the base payload.
3. Hover tooltips stay exactly as they are where they already exist
   (`renderHistogram`, `renderDurationHistogram`, heatmap). The click affordance
   is additive; it does not replace or alter any tooltip.
4. Drawer rows are read-only: thumb, title, channel, stats, and a rank badge. NO
   star, note editor, or Track button (those are write-path or cross-page state;
   keep them on the Board only).
5. Drawer rank is a 1-based position over the drilled subset, sorted by
   `views_to_subs_ratio` DESC, the same metric the Board ranks on (confirmed the
   same calc across all lanes, including Overall). NULL ratios sort last; ties
   break deterministically (`view_count` DESC, then `video_id`). Because
   `views_to_subs_ratio` is a single `videos` column, not a per-run value, there
   is one ratio per video and therefore no multi-run collapse rule to decide. The
   drawer's rank order matches the RATIO each row displays. The endpoint computes
   the position and returns it as `rank` so the shared renderer's badge just
   works.
6. Scope is the Dashboard page count charts only. The Trends view-count
   distribution is OUT of scope (see Out of scope). The publish-times heatmap is
   deferred (see Deferred).

## Governing contract (the one verify that gates every chart)

The drawer's row count MUST equal the clicked bar's count. Both must run over the
same deduped lane+window video set with the same predicate that produced the
count. If they disagree, the predicate is wrong. This equality is the per-facet
acceptance check at Gate 2 and Gate 4.

Critical: the bar counts are computed in PYTHON, not SQL. `_dashboard_video_rows`
(db.py ~2154) dedupes by lane bucket plus window and takes NO facet; the matching
count function then post-filters that row set in Python. So the drill MUST reuse
the SAME Python predicate that produced the count, never a fresh SQL `WHERE` or
`LIKE`. A substring `LIKE` over pipe-delimited tokens silently diverges from the
split-element tally. Precedent already in the repo: db.py ~1903 does
"split-element membership, never substring."

## Facet taxonomy

Every in-scope clickable count surface and the predicate that resolves a bar back
to its videos. Scatter (likes vs comments) and the title-anatomy KPI strip are
NOT count bars and are out of scope.

| Surface | facet key | value passed on click | predicate (Python, reuses the count's own code) |
| --- | --- | --- | --- |
| Topic mix, sub-niche volume | `subniche` | matched_queries token | `_split_pipe(matched_queries)` membership |
| Topic mix, YouTube category | `category` | resolved category title | `_category_label(category_id, title_by_id) == value`, where `title_by_id` is built from the FULL categories table (shared `_category_titles`), never the drilled subset |
| Topic mix, topic tags | `topic` | topic_categories token | `_split_pipe(topic_categories)` membership |
| Breakout, ratio bands | `ratio_band` | band label | `_ratio_band_for(ratio) == label` |
| Format, duration distribution | `duration_bin` | numeric range `lo`,`hi` | `lo <= duration_seconds < hi`, last bin inclusive of the top edge |

Sharp edges:

- `category` is the trap. The chart resolves `category_id` to `categories.title`
  and folds NULL or unknown into one "Uncategorized" bar (db.py
  `fetch_dashboard_topic_mix`, ~2210). The drill MUST invert that exact mapping,
  Uncategorized included, or count and list diverge. Concretely: extract the fold
  into a shared `_category_label(category_id, title_by_id)` helper used by BOTH
  the count and the drill, and build `title_by_id` from a shared
  `_category_titles(conn)` that reads the FULL categories table. Both sides must
  be fed that same full-table map; building it from the drilled subset's
  `category_id` values makes a category present in the count but absent from a
  facet's rows resolve differently, and count != list.
- `subniche` and `topic` are many-to-many: one video appears under several bars
  because `matched_queries` / `topic_categories` are pipe-delimited. The drawer
  list is "videos tagged with this value," not an exclusive slice. The header
  must say so. The count tally MUST dedupe tokens within a video
  (`set(_split_pipe(field))`), so a doubled token (`"high protein|high protein"`)
  counts that video ONCE, matching the drill's membership test. This changes a
  shipped count path: it is output-identical on real data only if no live row has
  doubled tokens, so the smoke run counts any row where a set-split differs in
  length from a raw split (expect 0); if non-zero, STOP and surface it as a
  separate decision before proceeding.
- `duration_bin` has NO server-side bins and NO config bin edges. The server
  returns raw `durations`; the bins and the bar count are computed in the browser
  (charts.js ~509, constants `DURATION_BIN_SECONDS` / `DURATION_MAX_SECONDS`,
  inclusive top edge so a 180s video lands in the last bin). Do NOT duplicate
  those constants server-side. The clicked bar already knows its range for its
  tooltip; it passes `lo`/`hi` and the server filters by range, replicating the
  inclusive-top-edge rule for the final bin exactly. Verify is asymmetric here:
  there is no server count to compare against (the payload carries raw durations,
  not a bar count), so parity is checked against the JS bin tally.

## Shared row renderer

Single source of truth for the row. Today the markup exists in two places:

- `board.js` `rowNode` (~154): the full `<article class="row">` with rank badge,
  thumb, title, channel, stats, inline note editor, star toggle, Track button.
  Helpers: `stat`, `rankBadge`, `fmtInt`, `fmtRatio`, `fmtDate`, plus the thumb
  and title builders. Note its metric stat already falls back to
  `views_to_subs_ratio` when `metric_value` is absent, which is exactly the
  dashboard drill case.
- `dashboard.js` `leaderboardTable` (~168): a hand-rolled near-Board row used by
  the Breakout leaderboard.

Gate 1 extracts the Board row into a shared module (`video-row.js`) parameterized
by mode. Board imports it and renders pixel-identically.

`leaderboardTable` consolidation onto the shared renderer is a NOTED FOLLOW-UP,
not in this critical path: its row shape differs and migrating it changes the
Breakout look. Keep scope surgical; consolidate later if wanted.

### Affordance modes

- `full` (Board): rank, thumb, title, channel, stats, note editor, star, Track.
  Unchanged from today.
- `readonly` (drawer): rank, thumb, title, channel, stats. No note editor, no
  star, no Track. (Differs from `full` only by dropping the three write-path
  actions; the rank badge stays.)

## Column contract for the lazy endpoint

The endpoint returns the exact columns the shared renderer reads, so the renderer
works with no per-caller shaping. For the read-only drawer the needed set is:
`rank` (the positional value the endpoint computes), `video_id`, `title`, `link`,
`thumbnail_url`, `channel_title`, `views_to_subs_ratio`, `view_count`,
`subscriber_count`, `views_per_day`, `published_at`. NO `metric_value`, NO
`starred`, NO `user_notes`.

Provenance, confirmed against code: `subscriber_count` is not on `videos`; it
comes from `channels` via `LEFT JOIN channels c ON c.channel_id = v.channel_id`,
which `fetch_dashboard_breakout` already uses (db.py ~2239). Everything else is a
`videos` column reachable through `_dashboard_video_rows` with that extra join.
`rank` is not stored; the endpoint computes it as the 1-based position after
sorting the filtered rows by `views_to_subs_ratio` DESC (Decision 5).

## Drawer spec

- Mounted ONCE at app level, not per page or per chart, so behavior is identical
  everywhere it is used.
- Right-anchored overlay with a backdrop. No page reflow.
- Header names the bar: value, count, lane (for example "high protein, 152
  videos, Health"). For many-to-many facets the header wording makes clear the
  list is videos tagged with the value, not an exclusive partition.
- Own vertical scroll for long lists. Independent of page layout.
- Close on the close control, backdrop click, and Esc. Focus trap while open;
  focus returns to the bar that opened it on close.
- Reduced-motion disables the slide transition.
- Narrow viewport: full-width sheet, since "sides" stop meaning anything. The
  drawer width and the narrow breakpoint come from tokens, NOT literals.
- Loading, empty, and error states inside the drawer, in the interface voice.

## Endpoint shape

`GET /api/dashboard/videos` keyed on `lane`, `start_date`, `end_date`, `facet`,
plus the facet argument:
- `subniche` / `category` / `topic` / `ratio_band`: a `value` string.
- `duration_bin`: numeric `lo` and `hi` instead of `value`.

One db helper: call `_dashboard_video_rows(lane, window)` selecting the board-row
columns plus the channels join, apply the matching Python predicate from the
taxonomy, sort by `views_to_subs_ratio` DESC, assign the positional `rank`, return
the rows.

## Click wiring

ECharts instances expose a click event. The renderer attaches
`inst.on("click", ...)`; for `renderBars` the clicked bar's yAxis category is the
facet value (charts.js ~350). For `duration_bin` the renderer already computes
the bar's `lo`/`hi` for its tooltip (charts.js ~514) and passes those. Each
renderer emits a normalized payload and calls the shared `openDrawer`. The
existing tooltip formatter is left untouched.

`renderBars` does not currently return its instance. CC either returns it or
attaches the handler inside the renderer; pick one and keep it uniform.

## Gate plan

Each gate ends in STOP. CC writes the implementation plan for the gate, gets it
reviewed, then codes. The verify triad runs at every gate: CC's unit tests pass,
CC runs the technical/smoke checks and commits, then Mary validates in the
browser before the next gate starts. Unit tests fold into the existing
`plan-dashboard-validation-harness.md` layout; CC confirms the test location and
runner rather than spinning up a parallel one. Some seams are JS-side (bin range,
click payload); CC confirms whether a JS test harness exists. If not, those seams
are covered by Mary's browser checks, not skipped.

- Gate 0 (no code).
  - Build: confirm the taxonomy and the count-equals-list contract translate
    cleanly into CC's Gate 1 plan.
  - Mary validates: review CC's Gate 1 plan.
  - STOP.

- Gate 1. Extract the Board row into `video-row.js` (`full` and `readonly`
  modes). Pure refactor, no behavior change.
  - Unit tests: if a JS/DOM harness exists, a rendered-row snapshot in each mode
    (`full` keeps star/note/Track; `readonly` drops them, keeps rank). If not,
    this gate is regression-checked by Mary below.
  - CC verify: `board.js` no longer hand-rolls the row; the Board imports the
    shared renderer; no console errors.
  - Mary validates (browser): the Board is visually and behaviorally identical.
    Rank, thumb, title, channel, stats render the same. Star toggles and persists
    across reload. Note saves and persists. Track adds the video to Trends and the
    5-video cap still disables a sixth row. Nothing shifted.
  - STOP.

- Gate 2. Lazy endpoint per Endpoint shape, backed by one db helper that reuses
  `_dashboard_video_rows` plus the matching Python predicate (NOT SQL `LIKE`),
  returning the column contract and the computed positional `rank`. The plan
  touching db.py DECLARES "no schema change."
  - Unit tests (pytest, seeded in-memory SQLite fixture):
    - Predicate parity: for every label in `fetch_dashboard_topic_mix`, the drill
      length for that label equals its count, for `subniche`, `topic`, and
      `category`. A multi-tag video appears under each of its tags (many-to-many),
      not consumed by the first.
    - Pipe-field edges: a video with a NULL/empty tag field appears in no bar and
      no drill, on both sides. A video with a doubled token counts ONCE in the bar
      (post `set()` dedupe) and appears ONCE in the drill: count == list == 1. This
      locks both the count's `set()` dedupe and the drill's membership.
    - Category inversion: a video whose `category_id` is missing from `categories`
      lands in "Uncategorized"; the Uncategorized drill length equals its count.
      Plus: topic-mix category counts are byte-identical before and after the
      `_category_label` / `_category_titles` extraction (the refactor is
      behavior-preserving).
    - `ratio_band`: drill length equals the band count; NULL ratio excluded; edge
      values land per `_ratio_band_for`.
    - `duration_bin`: range filter with the inclusive top edge, a 180s video lands
      in the last bin, a boundary value lands in the upper bin (floor semantics);
      parity with a fixed fixture's JS tally.
    - Rank: over the sorted subset, ranks are contiguous 1..N with NULL ratios
      last and a deterministic tie-break (`view_count` DESC, then `video_id`); the
      order matches `views_to_subs_ratio` DESC.
    - Endpoint: empty subset returns 200 and `[]`; unknown facet returns 4xx; the
      response carries every column-contract key.
  - CC verify: tests green; smoke the endpoint against a `VACUUM INTO` copy bound
    to 127.0.0.1, never the live WAL; print the parity table (label, bar count,
    drill length) for one lane+window. Also count any live row where
    `len(set(_split_pipe(f))) != len(_split_pipe(f))` for `matched_queries` and
    `topic_categories` (expect 0, proving the `set()` dedupe changes no live bar
    count); if non-zero, STOP and surface it.
  - Mary validates: backend-only gate, no UI yet. Review the parity-table smoke
    output and the passing test names, not the browser.
  - STOP.

- Gate 3. Shared overlay drawer mounted at app level per the Drawer spec. Wire
  ONE chart end to end (Topic mix, sub-niche): renderer emits the facet payload on
  click, calls `openDrawer`, tooltip untouched.
  - Unit tests: if a JS harness exists, the renderer emits the correct
    `{facet, value}` for a clicked bar. Else covered by Mary below.
  - CC verify: tests green; no console errors; the diff to the renderer shows only
    an added click handler, the tooltip formatter is unchanged.
  - Mary validates (browser): click a sub-niche bar. The drawer slides in from the
    right over a backdrop with no page reflow. The header shows value, count, lane.
    The drawer row count equals the bar's number. Rows match the Board format
    including the rank badge. Hover tooltip still works. Close via the X, backdrop
    click, and Esc; focus returns to the clicked bar. Narrow window gives a
    full-width sheet; reduced-motion gives no slide.
  - STOP.

- Gate 4. Roll out the remaining in-scope surfaces: `category`, `topic`,
  `ratio_band`, `duration_bin`.
  - Unit tests: the Gate 2 parity suite already covers these predicates; confirm
    the Uncategorized case and the duration top-edge case are present.
  - CC verify: parity tests green for every facet.
  - Mary validates (browser): repeat the Gate 3 drawer checks on each facet.
    Specifically click the Uncategorized category bar and a duration bin at the
    180s edge, and confirm the drawer count equals the bar count in each.
  - STOP.

## Out of scope

- Trends view-count distribution (`dist_bucket`). It is a different backend
  (app.py `api_distribution` ~467), per single run not lane+window, bucketed by
  `config.DISTRIBUTION_BUCKETS`, and it already embeds `videos` per bucket. It
  does not fit the `_dashboard_video_rows` endpoint, and its semantics are
  entangled with the unresolved period-aware phase (it is "this run" today).
  Revisit alongside that phase, not here.

## Deferred

- Publish-times heatmap (`publish_slot`). Each cell count is built by converting
  `published_at` to Eastern per row via `_parse_instant` and bucketing into
  weekday plus hour (fetch_dashboard_format), not a SQL date function. A drill
  must replicate that exact conversion or cells drift. Lowest value-to-effort of
  the surfaces; pull it in later if wanted.

## House rules carried into every gate

- Feature branch off the current work branch.
- No em dashes anywhere in code, comments, commits, or UI.
- No magic numbers and no magic literals; drawer width, breakpoint, and any sort
  keys come from config or tokens, never inline. Duration bin constants stay in
  the JS layer (single source); the server receives a range, it does not redefine
  the bins.
- Single source of truth: the row renders from `video-row.js` only.
- Deterministic over LLM; this feature has no LLM path.
- run_date stays a zero-padded Eastern date key compared lexically; the window
  passes through as `start_date` / `end_date` exactly as the dashboard endpoints
  already do.
- Any db.py-touching plan declares "no schema change" explicitly.
- Smoke tests run against a `VACUUM INTO` copy, never the live WAL DB.
- macOS BSD grep needs `-E`. Treat all cited line numbers as approximate and
  re-grep before editing. NOTE: the db.py line cites in this doc predate the
  Lifecycle Gate 1 commit (`2c202fd`), which appended `fetch_dashboard_lifecycle`
  and shifted everything below it down by roughly 150 lines. Re-grep every anchor
  on the current branch before touching code.