# Price-Refresh Agent: Feature Plan (v3)

Status: scoped, ready to build. Separate feature from the interpretation
generator. One feature, four phases (0-3). Test cases and manual validation steps
are inline at the end of each phase. Build order is sequential.

Changelog (v2 -> v3):
- STORAGE PIVOT: the generator no longer reads TOML prices; as of v7 the live
  source is `model_prices` (a date-windowed table in `swipefile.db`). v3 drops the
  entire `prices.toml` apparatus from v2: no `prices.toml`, no `safe_write_prices`,
  no `tomli-w`, no atomic-file/backup logic, no generator read-path change, no
  `.gitignore` bootstrap. Auto-apply now opens a new `model_prices` window via the
  existing `db.insert_price_window()`.
- TABLES LOCATION RESOLVED: `price_proposals` lives in `swipefile.db` (schema
  version bump), colocated with `model_prices`. The dedicated-file lean from v2 is
  reversed — auto-apply already writes `swipefile.db`, so splitting the workstream
  across two DBs added nothing.
- `price_history` TABLE DROPPED: `model_prices` is already date-windowed history.
  The previous active window IS yesterday's value (the day-over-day diff), and
  "rollback" is closing today's window. The separate snapshot table is redundant.

Carried forward from v2 (unchanged):
- Cross-source check has three outcomes (agree / lagging-source-passthrough /
  genuine-conflict reject), so a real move with one lagging source is not discarded.
- `price_proposals` has a unique-pending index + upsert so a persisting >10% move
  keeps exactly one pending row.
- Timezones pinned: run date is Eastern-local; `*_at` are ISO-8601 with Eastern
  offset.
- Validation thresholds live in a `[price_refresh]` block in `settings.toml`; layer
  signatures take them as params (no hardcoded defaults).

This is a daily, mostly-unattended agent that keeps the LLM price table current. It
fetches each provider's published prices, validates them through layered checks,
and either auto-applies a small change (a new `model_prices` window) or stages a
large one for a one-click human confirmation on the dashboard. The interpretation
generator is genuinely unchanged: it already reads `model_prices`, and this agent
only ever opens new windows or stages proposals under strict rules. Secondary goal:
this is a deliberate demonstration of an agent capability (goal + tools + strict
output contract + layered guardrails + human-in-the-loop gate), so the validation
architecture is a first-class part of the design, not an afterthought.

---

## Frozen contract

- The agent runs DAILY and unattended. An unattended job cannot pause for
  confirmation mid-run, so its job is DETECT-AND-STAGE, not commit-on-judgment.
  Three outcomes per price, in this exact precedence:
  1. REJECT (bad data): failed structural parse, magnitude/unit out of band, or
     genuine cross-source conflict (both sources disagree AND neither anchors to
     the last-kept value). The value is discarded and logged/alerted; it is NEVER
     routed to a human as if it were a real change. A 1000x unit error is bad
     extraction, not a big price move. NOTE: a single lagging source on a real-move
     day is NOT a reject — see the cross-source bullet below.
  2. AUTO-APPLY (sane and small): all sanity layers pass AND the change versus
     the current active price is under 10%. Applied with no human by opening a new
     `model_prices` window (the prior window remains as history).
  3. STAGE FOR REVIEW (sane and large): all sanity layers pass but the change is
     10% or more. NOT applied; staged as a pending proposal with citations,
     surfaced on the dashboard for a one-click confirm/reject. This is the ONLY
     case that needs a human.
- Cross-source disagreement is only a REJECT when neither source anchors to the
  last-kept value. If one source matches last-kept (within tolerance) and the other
  shows a large move, that is a lagging source on a real-move day: the MOVED value
  is passed through to the delta band, not discarded.
- The magnitude/unit sanity check runs BEFORE the 10% delta band, so nonsense is
  rejected outright instead of being mistaken for a large legitimate change.
- The 10% band FLAGS, it does not reject. Real LLM prices move in large discrete
  jumps (e.g. the documented 67% Opus cut, the ~58% Grok output drop), so a hard
  reject on a big move would throw away the most important update of the year. A
  real cut clears the human gate in one glance.
- Baseline = the current active `model_prices` window. That known-good value is the
  baseline for both the 10% comparison and the dashboard's day-over-day diff.
- Source of truth is `model_prices` (date-windowed, in `swipefile.db`): the
  generator already reads it, unchanged. A `price_proposals` table (also in
  `swipefile.db`) holds staged large changes. The generator never reads
  `price_proposals`; cost math has one unambiguous source.
- History is INHERENT to `model_prices`: each auto-apply opens a new window, so the
  previous window is yesterday's value with no separate snapshot table. "Yesterday"
  always has a value to diff against because windows are contiguous.
- Auto-apply is reversible: because the prior window is retained as history, a
  wrong-but-sub-10% value is rolled back by closing today's window (reverting to the
  prior active window) once seen in the day-over-day display.
- The dashboard shows: today's active price, the change from the prior window per
  model, and any pending proposals (the >10% moves) with a confirm/reject control.
- Network is real at runtime (no offline constraint on this project). Tests are
  deterministic via GOLDEN FIXTURES: saved page snapshots plus hand-verified
  expected JSON; the dev loop mocks the fetch and scores extraction accuracy.

Honest ceiling: this cannot be provably-correct with zero humans. The design
shrinks how often a human is needed (only sane >10% moves) and makes that touch a
10-second cited-diff review, not a full re-verification.

---

## Phase 0: storage, validation layers, safe writer (no agent, no network)

Goal: the one new table, the pure validation functions, and the auto-apply seam
onto the existing `model_prices` writer. After this phase the data model and every
guard exist and are unit-tested, but nothing fetches or calls a model.

### Scope

- One new table in `swipefile.db` (colocated with `model_prices`). Bump the DB
  schema version and follow the existing `IF NOT EXISTS` + `user_version` migration
  path. No `price_history` table — `model_prices` windows already are the history.

  ```
  CREATE TABLE IF NOT EXISTS price_proposals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      model TEXT,
      field TEXT,              -- 'input' or 'output'
      old_value REAL,
      new_value REAL,
      pct_change REAL,
      direction TEXT,          -- 'up' or 'down'
      quote TEXT,              -- the extracted snippet justifying the number
      source_url TEXT,
      status TEXT,             -- 'pending' / 'confirmed' / 'rejected'
      proposed_at TEXT,        -- ISO-8601 with Eastern offset
      resolved_at TEXT         -- ISO-8601 with Eastern offset
  )

  -- at most one pending proposal per (model, field): a persisting >10% move
  -- upserts this single row rather than stacking a new pending one each day.
  CREATE UNIQUE INDEX IF NOT EXISTS ux_pending_proposal
      ON price_proposals (model, field) WHERE status = 'pending';
  ```

  `proposed_at` / `resolved_at` are Eastern-anchored ISO-8601; the run date used for
  windowing follows whatever `model_prices` already uses (confirm convention below).

- Validation thresholds live in a `[price_refresh]` block in `settings.toml`
  (version-controlled static config, read via stdlib `tomllib`):

  ```toml
  [price_refresh]
  magnitude_lo    = 0.02    # per-MTok floor
  magnitude_hi    = 200.0   # per-MTok ceiling
  cross_tol       = 0.05    # cross-source agreement tolerance
  delta_threshold = 0.10    # auto-apply vs stage-for-review boundary
  ```

- Validation layers as PURE functions (no I/O), each independently testable,
  composed in this order so each gates the next. Thresholds are passed in (no
  hardcoded defaults that hide the source):
  - `check_structure(payload, canonical_models)`: parses to JSON, keys equal the
    canonical `provider:model` set exactly (no extra, no missing), every value a
    positive float. Returns ok / failure with the offending key.
  - `check_magnitude(prices, lo, hi)`: every input/output per-MTok value within the
    band. Catches the 1000x unit error and per-token vs per-MTok confusion.
    Failure -> reject.
  - `check_output_gt_input(prices)`: SOFT. Output should exceed input (true for
    all current budget models). A violation flags for review, does not reject, so
    a genuine future inversion is not silently dropped.
  - `check_cross_source(prices_a, prices_b, last_kept, tol)`: compares two
    independent sources per value, with THREE outcomes (not two):
    - Agree within `tol` -> pass.
    - Disagree, but ONE source matches `last_kept` within `tol` and the other shows
      a large move -> NOT a reject. Lagging source on a real-move day: pass the
      MOVED value through to `classify_delta` (which routes it to REVIEW). Record
      which source moved.
    - Disagree, and NEITHER cleanly matches `last_kept` -> REJECT (genuine garbage;
      independent sources rarely share the same wrong number, and neither anchors
      to known-good).
  - `classify_delta(new, last, threshold)`: per price, returns AUTO when movement
    is under threshold, REVIEW at or above. Carries pct and direction.
- `classify_price(...)`: composes the above into the three outcomes
  (REJECT / AUTO / REVIEW) with the precedence in the contract.
- `apply_price(model, field, new_value, source)`: opens a new `model_prices` window
  via the EXISTING `db.insert_price_window()`. No file writing, no backup files, no
  `tomli-w` / `tomlkit` — the date-windowed table is the durable record and its
  prior window is the rollback target. This is a thin seam over the existing writer,
  not a new persistence layer. Idempotency on same-day re-runs depends on
  `insert_price_window()` semantics — see the open decision below; the seam must not
  open a duplicate window for an unchanged price.

Files: a new module (e.g. `price_refresh/validate.py`, `price_refresh/store.py`
for the proposals table + the `apply_price` seam), the `price_proposals` schema +
migration, tests. The validation layer (`validate.py`) stays pure and has no DB
dependency, so it does not import anything heavy into the dashboard.

### Test cases

- Each validation layer in isolation: a clean payload passes; a missing key, an
  extra key, and a non-float each fail `check_structure` naming the key; a 1000x
  value and a per-token value fail `check_magnitude`; an inverted price flags (not
  rejects) in `check_output_gt_input`; a 7% move is AUTO and a 40% move is REVIEW in
  `classify_delta` with correct direction.
- `check_cross_source` three outcomes: sources agreeing within `tol` pass; sources
  disagreeing where ONE matches `last_kept` and the other shows a large move pass
  the MOVED value through (NOT reject); sources disagreeing where NEITHER matches
  `last_kept` reject.
- `classify_price` precedence: a value that is BOTH >10% AND a unit error is
  REJECTED (magnitude wins over the delta band), proving order matters.
- `apply_price`: opening a window for a model writes a new `model_prices` row via
  `insert_price_window()`; the prior window is retained (queryable as the previous
  value). Per the confirmed `insert_price_window()` semantics, a same-day re-apply
  of an UNCHANGED price does not create a duplicate window.
- Tables: fresh creation + migration bumps `user_version`; a `price_proposals`
  insert round-trips status; a second pending insert for the same `(model, field)`
  upserts the existing pending row (ux_pending_proposal holds — no duplicate
  pending).

### Manual validation

1. `pytest` for the feature: green.
2. In a REPL, run `classify_price` over a hand-built payload that mixes a small
   change, a 50% drop, and a unit error; confirm AUTO / REVIEW / REJECT
   respectively.
3. Call `apply_price` against a scratch copy of `swipefile.db`, then query
   `model_prices` and confirm a new window opened with the new value and the prior
   window is still present as history. Re-call with the same value and confirm no
   duplicate window per the `insert_price_window()` contract.

---

## Phase 1: extraction agent + golden fixtures (the agent capability)

Goal: the LLM-driven extraction that reads the pricing sources and returns a
strict, cited, structured price payload, plus the frozen fixtures that make
iteration measurable.

### Scope

- `extract_prices(sources) -> ExtractionResult`: an agent call (LLM with
  web_fetch / web_search tools) over each provider's pricing source. Strict output
  CONTRACT enforced by the prompt: return JSON ONLY, keyed by the canonical
  `provider:model` strings, each value `{input, output, quote, source_url}` where
  `quote` is the exact snippet the number came from and `source_url` is the page.
  No prose, no markdown. The model reads MEANING (robust to layout changes) but
  the citations make every number checkable.
- Two independent sources per provider (official page plus an aggregator, or two
  aggregators) so `check_cross_source` has something to compare. The agent must
  extract STANDARD per-MTok input/output rates, explicitly not batch, cached, or
  image rates (a named failure mode in the prompt).
- Robust JSON handling: strip stray fences, parse defensively, surface a clear
  failure if the model returned unparseable output (which then becomes a REJECT
  upstream, never a guess).
- GOLDEN FIXTURES: saved snapshots of each source page plus a hand-verified
  expected JSON per source, committed to the repo. These are the oracle. A scorer
  computes extraction accuracy (N of M prices correct) against expected.

Files: `price_refresh/extract.py`, `price_refresh/fixtures/` (snapshots +
expected), tests.
  
### Test cases

- Dev loop (mocked fetch against fixtures, deterministic): `extract_prices`
  returns the expected JSON for each frozen snapshot; the scorer reports 100% on
  the golden set (or the known-correct baseline). This is the regression gate for
  every future prompt change.
- Contract enforcement: a fixture where the page also shows batch/cached rates;
  assert the agent picks the STANDARD rate, not the discounted one.
- Citation presence: every extracted price carries a non-empty `quote` and a
  `source_url`; a missing citation fails the contract check.
- Bad-output handling: a fixture returning prose or broken JSON yields a clean
  extraction failure, not a partial/guessed payload.
- Live smoke (`@pytest.mark.live`, excluded by default): hit the real sources for
  one provider, assert a parseable cited payload comes back; HARD-FAIL with a
  clear message on network/parse failure.

### Iteration loop (how "correct" is measured)

- DEV loop: agent against frozen snapshots, scored against hand-verified expected.
  Fast, offline, repeatable. A prompt tweak that lowers the score is a regression;
  this is the entire dev feedback cycle.
- LIVE loop: agent against the real web, feeding the Phase 2 validation layers and
  producing proposals. Same agent, different input and a human gate downstream.

### Manual validation

1. `pytest` dev-loop tests: green, 100% on the golden set.
2. Refresh the snapshots from the live pages, re-hand-verify the expected JSON,
   re-run the scorer; confirm it still scores correctly (this is the periodic
   fixture-maintenance ritual).
3. `pytest -m live` for one provider: a real cited payload returns.

---

## Phase 2: daily orchestration script

Goal: the unattended daily job that ties extraction, validation, classification,
auto-apply (open a `model_prices` window), and staging together under the contract
rules.

### Scope

- `run_daily()`:
  1. Load the current active price per model from `model_prices` (the baseline).
  2. `extract_prices` from both source sets (live).
  3. For each price, `classify_price` (reject / auto / review) using the layers
     and the active-window baseline.
  4. REJECT: log + alert, open no window, stage nothing for that price.
  5. AUTO: `apply_price` -> open a new `model_prices` window via
     `insert_price_window()`. The prior window remains as history.
  6. REVIEW: UPSERT the pending `price_proposals` row for `(model, field)` — if one
     is already pending, refresh its `new_value`, `pct_change`, `direction`,
     `quote`, `source_url`, `proposed_at` in place; otherwise insert. Never stack a
     second pending row for the same model/field (a persisting >10% move keeps one
     pending row until a human acts). Open no window.
  7. No separate snapshot step. History is inherent: AUTO opened a new window;
     no-change and staged/rejected models simply keep their existing active window,
     which already serves as the diff baseline.
  8. Print/emit a run summary (applied / staged / rejected counts).
- Scheduling is external (cron / launchd / Cowork task); the script is
  idempotent-safe to re-run a given day: `insert_price_window()` does not open a
  duplicate window for an unchanged price (per its confirmed semantics) and
  `ux_pending_proposal` keeps proposals deduped, so a second run neither
  double-applies nor stacks proposals.
- A confirm path the dashboard calls (Phase 3 wires the UI): `confirm_proposal(id)`
  applies the staged value via `apply_price` (opens a window), marks the proposal
  confirmed; `reject_proposal(id)` marks it rejected and opens no window.

Files: `price_refresh/daily.py`, tests.

### Test cases

- End-to-end with extraction mocked to a fixture and the web stubbed: a small
  change auto-applies (a new `model_prices` window opens, prior window retained); a
  40% change stages a pending proposal and no window opens; a unit error is rejected
  (no window, no proposal, alert logged).
- No-change day: opens no new window; the existing active window remains the
  baseline, so the dashboard diff still resolves.
- Idempotency: running `run_daily()` twice for the same date does not open a
  duplicate window for an unchanged price and does not stack proposals.
- Persisting move: a >10% move that stays unconfirmed across two days keeps exactly
  ONE pending proposal (second run upserts the row, does not stack a duplicate).
- Lagging source: a fixture where source A shows the moved price and source B still
  matches the active window stages a REVIEW (not a reject), proving the cross-source
  passthrough.
- `confirm_proposal` opens a window and marks confirmed; `reject_proposal` opens no
  window and marks rejected.
- Reversibility: after an auto-apply, the prior value is recoverable from the prior
  `model_prices` window (close today's window to revert).

### Manual validation

1. `pytest`: green.
2. Run `run_daily()` once against live sources in a scratch copy of `swipefile.db`:
   inspect the run summary; `model_prices` has a new window only for sub-10% moves;
   any >10% move appears in `price_proposals` as pending with a quote and URL.
3. Re-run for the same day: confirm no duplicate window for unchanged prices and no
   double-apply.
4. Manually call `confirm_proposal` on a pending row: a new `model_prices` window
   opens; the generator's displayed cost would now use the new value.

---

## Phase 3: dashboard display + manual run + model control

Goal: show today's prices, the day-over-day change, and the pending >10% proposals
with a one-click confirm/reject; ALSO let a human trigger a run on demand, with a
chosen extraction model and a visible timer, mirroring the interpretation page's
run controls.

### Blocking contract change (explicit, not silent)

The dashboard is read-only-strict except where the interpret Phase 3 amendment
already carved out the interpret-write + spend-read endpoints. This phase AMENDS
`dashboard-plan.MD` again, on purpose, to permit the price-refresh endpoints: one
read (prices + deltas + pending proposals), two proposal writes (confirm/reject),
one run trigger (a write path that runs the agent: live fetch + auto-apply windows
+ proposal staging), and one model-set write (persist the active extraction
model). Tracked edit to that doc; everything else stays read-only.

### Active-extraction-model storage (the model the user sets)

The user sets the extraction model through the dashboard, and that choice persists
for BOTH the dashboard's manual runs and the unattended cron run. Storage rules:

- The active model is persisted in the existing key-value `app_preferences` table
  in `swipefile.db`, NOT `settings.toml`. `settings.toml` is version-controlled
  static config validated at load; a web request must never write it. This is a
  row upsert into an existing table: NO new table, NO new column, NO schema-version
  bump.
  - Key: one canonical key, `extraction_model`.
  - Value: the canonical `provider:model` string, same format as `settings.toml`
    `EXTRACTION_MODEL`, so the read path needs no translation.
  - CC: confirm `app_preferences`'s existing value convention (plain TEXT vs
    JSON-encoded vs per-type columns) and match whatever existing rows do; do not
    introduce a new encoding for this one key.
- `settings.toml` `EXTRACTION_MODEL` is the SEED DEFAULT only: read when
  `app_preferences` has no `extraction_model` row (first run). Once the user sets a
  model, the stored value wins.
- Read path (single, shared): `app_preferences['extraction_model']` if present,
  else `settings.toml` `EXTRACTION_MODEL`. `run_daily` and the dashboard both read
  through this one path, so the cron and the manual button never diverge.
- The model-set endpoint validates the choice against the registry (the `models`
  table) before upserting: it must be an existing capable `provider:model` row.
  Reject an unknown model with a clean inline error; never persist an unusable
  value.

### Scope

- Read endpoint(s): each model's current active `model_prices` value with the
  delta versus its prior window (value, percent, direction), and the list of
  pending `price_proposals`.
- A prices panel: per model, current input/output, and a "vs prior window" delta
  (e.g. "-3.0%" or "no change"). Auto-applied moves show here directly.
- A review panel: pending proposals (the >10% moves), each showing old -> new, the
  percent and direction, the quote, and the source link, with Confirm and Reject
  buttons calling `confirm_proposal` / `reject_proposal`.
- Model control (NEW): a dropdown of registry `provider:model` options (the capable
  / extractable models). On page load, prepopulate from the persisted active model
  (DB), falling back to `settings.toml` `EXTRACTION_MODEL` when unset. Changing the
  selection and saving calls the model-set endpoint, which validates and persists.
  Only the MODEL is exposed; temperature and seed are NOT user-controllable. The
  run always uses `temperature=0.0` + the fixed seed so the determinism contract
  and the golden-set baselines hold. A user-tunable temp/seed would let a dashboard
  click invalidate the live accuracy gate.
- Run button (NEW): fires a single POST that calls `run_daily()` using the active
  extraction model. Returns a run summary `{applied, staged, rejected, errors}`.
  The response refreshes the prices and review panels (auto-applied moves appear,
  new proposals populate the review list). On a provider/key/network error the
  endpoint returns a clean error payload and the UI shows an inline message, never
  a raw 500. `run_daily` is idempotent (Phase 2), so the button is safe to click
  even if cron already ran today: no duplicate windows, no stacked proposals.
- Timer (NEW): a client-side stopwatch, same pattern as the interpret page. Starts
  on button click, ticks the display (~100ms), freezes when the run summary lands,
  shows final elapsed. NOTE it times the WHOLE run (every provider x 2 sources:
  fetch + extract + validate + classify + write), not a single LLM call, so expect
  longer and more variable wall time than the interpret stopwatch. One run per
  click, one timer.
- Confirm/reject, run, and model-set are all write actions; surface clean inline
  errors, never a raw 500.
- The page these panels mount on is intentionally NOT pinned here (the Models page
  is the obvious candidate, live target TBD); the endpoints and panels are
  page-agnostic, so placement can be decided when the page is chosen.

### Endpoints (mirroring the interpret-button contract)

- GET price read: active prices + prior-window deltas + pending proposals. (read)
- POST `confirm_proposal(id)` / POST `reject_proposal(id)`. (writes; from prior
  Phase 3)
- POST run: empty body. The run uses the PERSISTED active model (no model in the
  body). Calls `run_daily()`; returns `{applied, staged, rejected, errors}`.
  (write)
- GET active model + POST set active model: body `{model}` (canonical
  `provider:model`). Validates against the registry, persists to `swipefile.db`.
  (read + write)

The run POST deliberately does NOT take a model in its body. The user sets the
model via the model-set endpoint first; the run uses the persisted active model.
This keeps the cron and the manual run reading one value and avoids a per-click
model that silently diverges from what the unattended job will use. (Alternative,
if a transient per-run override that does NOT persist is wanted instead: the run
POST takes an optional `model` used for that run only, cron stays on the persisted
default. Flag this if preferred; the default here is set-then-run against one
persisted value.)

### Files

`dashboard/app.py` (read + confirm/reject + run + get/set active model),
`dashboard/static/` (prices panel, review panel, model dropdown, run button,
stopwatch JS), `dashboard-plan.MD` (amendment), tests under the dashboard suite.
No migration: the active model is an `app_preferences` row upsert in an existing
table.

### Test cases

- TestClient: the prices endpoint returns correct prior-window deltas from seeded
  `model_prices` windows (including a "no change" model and an auto-applied move);
  the proposals endpoint lists only pending rows.
- Confirm opens a window for the staged value and the row leaves the pending list;
  reject opens no window and drops it from pending. Both return clean payloads.
- A model with only one window (no prior) renders "no prior data", not an error.
- Active-model set: a valid registry `provider:model` upserts the
  `app_preferences['extraction_model']` row and reads back; an unknown model is
  rejected with a named error and nothing is written; with no
  `extraction_model` row present, the read path returns `settings.toml`
  `EXTRACTION_MODEL` as the default.
- Run endpoint (`run_daily` stubbed at the fetch + generate seam): returns the
  `{applied, staged, rejected, errors}` summary; uses the PERSISTED active model
  (assert the model passed into the run matches the stored value, not
  `settings.toml`, when a value is set); a clean error payload on a stubbed
  provider/key failure, never a raw 500.
- Run uses `temperature=0.0` + the fixed seed regardless of the selected model
  (assert the run never passes a user-supplied temp/seed).
- Idempotency surfaced through the endpoint: two run POSTs for the same day do not
  double-apply or stack proposals (delegates to the Phase 2 `run_daily` guarantee;
  assert at the endpoint level).

### Manual validation

1. Dashboard tests green.
2. Open the page: the model dropdown shows the stored active model (or the
   `settings.toml` default when no `extraction_model` preference exists yet).
   Change it to `ollama:qwen3.5:9b`, save, reload; the selection persists from the
   `app_preferences` row.
3. With a couple of daily runs behind it, open the prices panel: deltas match the
   `model_prices` windows by hand; an auto-applied sub-10% move shows its change.
4. Click Run: the stopwatch runs and freezes on the summary; a new `model_prices`
   window appears for any sub-10% move; any >10% move appears in the review panel
   as pending with a quote and source. Confirm one (a window opens, the row
   clears); stage another and Reject (no window opens).
   5. Confirm the cron path reads the SAME active model: set the model on the
      dashboard, then run the cron entry point directly and confirm it uses the
      persisted model, not `settings.toml`.- 