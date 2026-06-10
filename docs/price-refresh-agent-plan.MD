# Price-Refresh Agent: Feature Plan (v1)

Status: scoped, ready to build. Separate feature from the interpretation
generator. One feature, four phases (0-3). Test cases and manual validation steps
are inline at the end of each phase. Build order is sequential.

This is a daily, mostly-unattended agent that keeps the LLM price map current. It
fetches each provider's published prices, validates them through layered checks,
and either auto-applies a small change or stages a large one for a one-click human
confirmation on the dashboard. The interpretation generator is unchanged by this
feature: it keeps reading `settings.toml [prices]`; this agent only ever proposes
or applies updates to that map under strict rules. Secondary goal: this is a
deliberate demonstration of an agent capability (goal + tools + strict output
contract + layered guardrails + human-in-the-loop gate), so the validation
architecture is a first-class part of the design, not an afterthought.

---

## Frozen contract

- The agent runs DAILY and unattended. An unattended job cannot pause for
  confirmation mid-run, so its job is DETECT-AND-STAGE, not commit-on-judgment.
  Three outcomes per price, in this exact precedence:
  1. REJECT (bad data): failed structural parse, magnitude/unit out of band, or
     cross-source disagreement. The value is discarded and logged/alerted; it is
     NEVER routed to a human as if it were a real change. A 1000x unit error is
     bad extraction, not a big price move.
  2. AUTO-APPLY (sane and small): all sanity layers pass AND the change versus
     the last-kept value is under 10%. Applied with no human, after a backup.
  3. STAGE FOR REVIEW (sane and large): all sanity layers pass but the change is
     10% or more. NOT applied; staged as a pending proposal with citations,
     surfaced on the dashboard for a one-click confirm/reject. This is the ONLY
     case that needs a human.
- The magnitude/unit sanity check runs BEFORE the 10% delta band, so nonsense is
  rejected outright instead of being mistaken for a large legitimate change.
- The 10% band FLAGS, it does not reject. Real LLM prices move in large discrete
  jumps (e.g. the documented 67% Opus cut, the ~58% Grok output drop), so a hard
  reject on a big move would throw away the most important update of the year. A
  real cut clears the human gate in one glance.
- Baseline = the last-kept price. The last-known-good values are the baseline for
  both the 10% comparison and the dashboard's day-over-day diff.
- Source of truth stays `settings.toml [prices]`: the generator reads it,
  unchanged. A `price_history` table records each daily run's kept values (the
  yesterday diff), and a `price_proposals` table holds staged large changes. The
  generator never reads either table; cost math has one unambiguous source.
- A dated snapshot of the kept values is persisted EVERY run, including no-change
  days, so "yesterday" always has a value to diff against.
- Auto-apply is reversible: before writing `settings.toml`, the prior `[prices]`
  values are recorded (history append + a file backup), so a wrong-but-sub-10%
  value can be rolled back once seen in the day-over-day display.
- The dashboard shows: today's kept price, the change from yesterday per model,
  and any pending proposals (the >10% moves) with a confirm/reject control.
- Network is real at runtime (no offline constraint on this project). Tests are
  deterministic via GOLDEN FIXTURES: saved page snapshots plus hand-verified
  expected JSON; the dev loop mocks the fetch and scores extraction accuracy.

Honest ceiling: this cannot be provably-correct with zero humans. The design
shrinks how often a human is needed (only sane >10% moves) and makes that touch a
10-second cited-diff review, not a full re-verification.

---

## Phase 0: storage, validation layers, safe writer (no agent, no network)

Goal: the tables, the pure validation functions, and the style-preserving
`settings.toml` writer. After this phase the data model and every guard exist and
are unit-tested, but nothing fetches or calls a model.

### Scope

- Two new tables (own DB or a small dedicated SQLite file for this feature;
  do NOT co-mingle with `swipefile.db` unless you decide otherwise, since this is
  a separate workstream). If reusing `swipefile.db`, bump its schema version and
  follow the same `IF NOT EXISTS` + `user_version` path; that is a decision to
  confirm before building, flagged here.

  ```
  CREATE TABLE IF NOT EXISTS price_history (
      snapshot_date TEXT,      -- the run's date, YYYY-MM-DD
      model TEXT,              -- canonical "provider:model"
      input_price REAL,
      output_price REAL,
      source TEXT,             -- where the kept value came from
      captured_at TEXT,
      PRIMARY KEY (snapshot_date, model)
  )

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
      proposed_at TEXT,
      resolved_at TEXT
  )
  ```

- Validation layers as PURE functions (no I/O), each independently testable,
  composed in this order so each gates the next:
  - `check_structure(payload, canonical_models)`: parses to JSON, keys equal the
    canonical `provider:model` set exactly (no extra, no missing), every value a
    positive float. Returns ok / failure with the offending key.
  - `check_magnitude(prices, lo=0.02, hi=200.0)`: every input/output per-MTok
    value within the band. Catches the 1000x unit error and per-token vs per-MTok
    confusion. Failure -> reject.
  - `check_output_gt_input(prices)`: SOFT. Output should exceed input (true for
    all current budget models). A violation flags for review, does not reject, so
    a genuine future inversion is not silently dropped.
  - `check_cross_source(prices_a, prices_b, tol=0.05)`: two independent sources
    agree within tolerance per value. Disagreement -> reject (independent sources
    rarely share the same wrong number, so this is the strongest cheap signal).
  - `classify_delta(new, last, threshold=0.10)`: per price, returns AUTO when
    movement is under threshold, REVIEW at or above. Carries pct and direction.
- `classify_price(...)`: composes the above into the three outcomes
  (REJECT / AUTO / REVIEW) with the precedence in the contract.
- `safe_write_prices(toml_path, new_prices)`: updates ONLY the `[prices]` block of
  `settings.toml` while preserving comments and the rest of the file. Use a
  style-preserving TOML writer (tomlkit); a plain dump (tomli-w) would strip the
  hand-edited comments and the verify-date line, so this is load-bearing. Writes a
  timestamped backup of the prior file first.

Files: a new module (e.g. `price_refresh/validate.py`, `price_refresh/store.py`),
schema for the two tables, tests.

### Test cases

- Each validation layer in isolation: a clean payload passes; a missing key, an
  extra key, and a non-float each fail `check_structure` naming the key; a 1000x
  value and a per-token value fail `check_magnitude`; an inverted price flags (not
  rejects) in `check_output_gt_input`; sources disagreeing past tolerance fail
  `check_cross_source`; a 7% move is AUTO and a 40% move is REVIEW in
  `classify_delta` with correct direction.
- `classify_price` precedence: a value that is BOTH >10% AND a unit error is
  REJECTED (magnitude wins over the delta band), proving order matters.
- `safe_write_prices`: writing new values preserves an existing comment line and
  the non-`[prices]` settings byte-for-byte; the backup file is created with the
  prior contents.
- Tables: fresh creation; a `price_history` upsert is idempotent per
  `(snapshot_date, model)`; a `price_proposals` insert round-trips status.

### Manual validation

1. `pytest` for the feature: green.
2. In a REPL, run `classify_price` over a hand-built payload that mixes a small
   change, a 50% drop, and a unit error; confirm AUTO / REVIEW / REJECT
   respectively.
3. Call `safe_write_prices` on a copy of `settings.toml`, open the result, and
   confirm comments and other settings survived and only `[prices]` numbers
   changed, and that a `.bak` (or timestamped) backup exists.

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
auto-apply, snapshotting, and staging together under the contract rules.

### Scope

- `run_daily()`:
  1. Load last-kept values (from `settings.toml [prices]`, equivalently the most
     recent `price_history` snapshot).
  2. `extract_prices` from both source sets (live).
  3. For each price, `classify_price` (reject / auto / review) using the layers
     and the last-kept baseline.
  4. REJECT: log + alert, do not touch the map or stage anything for that price.
  5. AUTO: back up `settings.toml`, `safe_write_prices` the new value, record the
     change.
  6. REVIEW: insert a `price_proposals` row (pending) with old/new, pct,
     direction, quote, source_url. Do NOT change the map.
  7. Append today's KEPT values to `price_history` for every model (new value
     where auto-applied, unchanged value where staged or rejected), so the
     yesterday diff is always populated.
  8. Print/emit a run summary (applied / staged / rejected counts).
- Scheduling is external (cron / launchd / Cowork task); the script is
  idempotent-safe to re-run a given day (the `price_history` PK is
  `(snapshot_date, model)`, so a second run upserts rather than duplicating).
- A confirm path the dashboard calls (Phase 3 wires the UI): `confirm_proposal(id)`
  applies the staged value via `safe_write_prices`, appends to history, marks the
  proposal confirmed; `reject_proposal(id)` marks it rejected and leaves the map.

Files: `price_refresh/daily.py`, tests.

### Test cases

- End-to-end with extraction mocked to a fixture and the web stubbed: a small
  change auto-applies (map written, backup made, history appended); a 40% change
  stages a pending proposal and the map is unchanged; a unit error is rejected
  (no map change, no proposal, alert logged).
- Snapshot-every-run: a no-change day still appends a `price_history` row per
  model.
- Idempotency: running `run_daily()` twice for the same date upserts history (no
  duplicate rows) and does not double-apply.
- `confirm_proposal` applies the staged value and marks confirmed;
  `reject_proposal` leaves the map and marks rejected.
- Reversibility: after an auto-apply, the prior values are recoverable from the
  backup / history.

### Manual validation

1. `pytest`: green.
2. Run `run_daily()` once against live sources in a scratch copy: inspect the run
   summary, `price_history` has today's row per model, `settings.toml` changed
   only for sub-10% moves, any >10% move appears in `price_proposals` as pending
   with a quote and URL.
3. Re-run for the same day: confirm no duplicate history rows and no double-apply.
4. Manually call `confirm_proposal` on a pending row: the map updates and history
   appends; the generator's displayed cost would now use the new value.

---

## Phase 3: dashboard display (change from yesterday + pending review)

Goal: show today's prices, the day-over-day change, and the pending >10% proposals
with a one-click confirm/reject.

### Scope

- Read endpoint(s): today's kept prices with the delta versus yesterday's
  `price_history` snapshot per model (value and percent, with direction), and the
  list of pending `price_proposals`.
- A prices panel: per model, current input/output, and a "vs yesterday" delta
  (e.g. "-3.0%" or "no change"). Auto-applied moves show here directly.
- A review panel: pending proposals (the >10% moves), each showing old -> new, the
  percent and direction, the quote, and the source link, with Confirm and Reject
  buttons calling `confirm_proposal` / `reject_proposal`.
- Confirm/reject are write actions (mirroring the interpret-button contract
  amendment); surface clean inline errors, never a raw 500.

Files: `dashboard/app.py` (read + two write endpoints), `dashboard/static/`
(prices panel, review panel), tests.

### Test cases

- TestClient: the prices endpoint returns correct yesterday deltas from seeded
  `price_history` (including a "no change" day and an auto-applied move); the
  proposals endpoint lists only pending rows.
- Confirm applies the staged value and the row leaves the pending list; reject
  leaves the map and drops it from pending. Both return clean payloads.
- A day with no yesterday snapshot renders "no prior data", not an error.

### Manual validation

1. Dashboard tests green.
2. With a couple of daily runs behind it, open the prices panel: deltas match
   `price_history` by hand; an auto-applied sub-10% move shows its change.
3. Stage a >10% move (or seed one), open the review panel, confirm the quote and
   source render; click Confirm and watch the map update and the row clear; stage
   another and Reject, confirm the map is untouched.

---

## Open decisions to confirm before building

- WHERE THE TABLES LIVE: a dedicated SQLite file for this feature, or new tables
  in `swipefile.db` (which means a schema-version bump and the same migration
  path). This is a separate workstream, so I lean a dedicated file to keep the
  seed DB's surface small, but it is your call.
- TOML-WRITE vs TABLE-AUTHORITATIVE: this plan keeps `settings.toml` authoritative
  and edits it in place with a style-preserving writer (tomlkit), so the generator
  is unchanged. The alternative is to make `price_history` the source of truth and
  have the generator read the latest snapshot, avoiding programmatic TOML edits
  entirely. I lean keep-TOML (smaller blast radius, generator untouched), but the
  in-place edit is the one fiddly part, so flagging the fork.

## Reuse / boundary notes

- The interpretation generator is NOT modified. It reads `config.PRICES` from
  `settings.toml`; this feature only updates that map under the contract rules.
- `estimate_cost` and the dashboard spend panel (generator Phase 4) automatically
  reflect any applied price change, with no migration, because cost is computed at
  display from the map.
- Live network is fine at runtime; determinism for tests comes from the golden
  fixtures, not from avoiding the network.
