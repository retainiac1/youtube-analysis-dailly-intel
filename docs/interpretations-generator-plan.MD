# Interpretation Generator: Feature Plan (v1)

Status: frozen contract, ready to build. One feature, five phases (0-4). Test
cases and manual validation steps are inline at the end of each phase. Build
order is sequential; nothing in a later phase is needed by an earlier one.

This document is the spec for the interpretation generator: the step that sends a
ranked lane to a user-selected LLM, gets a 3-4 sentence summary back, persists it
to `interpretations`, and logs the invocation (with the parameters that produced
it). The dashboard's read-only display of `interpretations` already exists
(dashboard plan Phase 4); this feature is the writer side the dashboard plan
deferred.

---

## Frozen contract

- Three scopes (the `compute_rankings` lanes): `overall`, `health`, `habit`.
  Lane and scope are the same value; the `interpretations.scope` column and the
  `rankings.bucket` column both hold it. v1 always synthesizes the FULL lane,
  unfiltered. Rail filtering is parked to v2, but the `filter` column is added
  now (see `llm_invocations`) so v2 can log a filtered run without a schema bump.
- Output: a 3-4 sentence summary per scope. An empty lane (zero ranked rows)
  skips the LLM call and writes no row; the dashboard's existing empty state
  covers it.
- One LLM per run. The user picks one model plus its parameters (temperature,
  optional seed) at the dashboard; every invocation in that run uses them. No
  concurrent providers, no parallel lanes in v1 (calls are sequential).
- Providers: Anthropic, OpenAI, xAI, Google (Gemini). One internal seam,
  `generate(provider, model, prompt, *, temperature, seed) -> GenerateResult`,
  with a thin adapter per provider. Keys come from `.env` via `load_dotenv()` +
  `os.getenv()`, matching the existing `YOUTUBE_API_KEY` convention; a missing
  key hard-fails with a clear, provider-named message (never a silent skip,
  never a bare 500).

- CANONICAL MODEL STRING (single source of truth). The combined string
  `provider:model` (e.g. `anthropic:claude-haiku-4-5`) is the one form used
  everywhere it is stored, selected, or priced: the dashboard selector value,
  the `interpretations.model` column, the `llm_invocations.model` column, and
  the `[prices]` map keys all use it. It is split into `(provider, model)` at
  EXACTLY ONE point: the entry to `generate()`, via a `split_model(s)` helper
  that splits on the first `:`. Adapters and the SDKs receive the split parts;
  nothing else does. `estimate_cost` keys on the combined string. This prevents
  the store-combined / look-up-split mismatch that would make every price lookup
  silently miss.

- PER-PROVIDER PARAMETER SUPPORT (handled in the adapters, where the provider is
  known):
  - `temperature`: supported by all four, but the valid RANGE differs by
    provider (Anthropic 0.0-1.0; OpenAI, xAI, Gemini 0.0-2.0). The adapter
    validates against its own provider's range and raises a clear, provider-named
    error if out of range. There is no single global range.
  - `seed`: supported by OpenAI, xAI, and Gemini; NOT supported by Anthropic's
    Messages API. The Anthropic adapter omits seed from the call. To keep the log
    honest (the column records what actually governed the generation),
    `llm_invocations.seed` is written NULL whenever the chosen provider did not
    apply a seed, regardless of what the user typed.

- Persistence: every successful call writes the canonical row to
  `interpretations(run_date, scope)` (insert-or-overwrite) AND appends one row to
  `llm_invocations` recording the full parameter set. `interpretations.model`
  holds the canonical `provider:model`.
- Tokens (input/output) are logged exactly from each provider's usage response.
  Dollar cost is NOT stored: no provider returns it, and a stored figure rots
  when prices change. Cost is computed at DISPLAY time from a `[prices]` map in
  `settings.toml`, labeled an estimate, shown "unavailable" for any model absent
  from the map.
- Selector + parameter defaults: on dashboard load, read the MOST RECENT
  `llm_invocations` row by `MAX(id)` (the autoincrement id is a monotonic
  recency proxy, so this avoids any lexical timestamp comparison) and
  prepopulate the model dropdown, temperature, and seed from it. On an empty
  table, default to the first model option, a default temperature, and no seed.
- Run targeting: a run operates on an existing `run_date`. Default is the most
  recent `run_date` present in `rankings` (`fetch_run_dates` returns these
  descending); a CLI flag can override it. The generator NEVER derives
  `run_date` from `now` (it summarizes a run the pipeline already wrote);
  `now_local_iso()` is used only for the `generated_at` timestamps. `run_date`
  is the Eastern calendar date and is never equated with `pacific_date`.
- DB access reuses `db.get_connection` (WAL on), `db.transaction` (commit/
  rollback inner), and `db.run_with_db_retry` (retry outer). Synthesize first
  (network), THEN open one short transaction to write, so no SQLite write lock
  is ever held across network I/O.
- Tests: a mocked suite is the default (fast, deterministic, free) and mocks the
  single `generate()` seam. A live smoke suite (`@pytest.mark.live`) is excluded
  by default and hits the real APIs; when run, it HARD-FAILS with a clear message
  if a key is missing or a provider errors (no skip). Live tests send a tiny
  payload purely to confirm a real response comes back.

Seeded `swipefile.db` is irreplaceable. Any step that writes to the live DB
takes a verified `VACUUM INTO` backup first (never `cp` a WAL-mode file, never a
casual `wal_checkpoint` on the seed).

---

## Phase 0: schema, price map, DB writers (no LLM, no network)

Goal: everything the generator writes to, with zero model involvement. After
this phase the DB can store interpretations and parameterized invocation logs,
and the price map exists and is validated, but nothing calls an LLM yet.

### Scope

- Bump `db.SCHEMA_VERSION` from 3 to 4. Add one `CREATE TABLE IF NOT EXISTS`
  statement to `SCHEMA_STATEMENTS` for `llm_invocations`. This is THE
  LLM-specific table: it records every call and the full parameter set that
  produced it.

  ```
  CREATE TABLE IF NOT EXISTS llm_invocations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      run_date TEXT,
      scope TEXT,
      model TEXT,            -- canonical "provider:model"
      temperature REAL,
      seed INTEGER,          -- NULL when unset OR provider does not apply a seed
      filter TEXT,           -- v2 forward-compat; ALWAYS NULL in v1
      input_tokens INTEGER,
      output_tokens INTEGER,
      generated_at TEXT
  )
  ```

  Append-only log (autoincrement id), not keyed by `(run_date, scope)`, so
  re-running a lane appends a new invocation row while overwriting the single
  interpretations row. Invocations are the parameter + spend history;
  interpretations is the current summary. No dollar column (cost is computed at
  display). The `filter` column is inert in v1 (written NULL) and exists so a v2
  filtered run can be logged without a schema change.

- The migration needs NO new branch. `init_db` already loops `CREATE TABLE IF
  NOT EXISTS` over fresh and seeded DBs and stamps `user_version` when it differs
  (the same non-destructive 2->3 path the schema comment documents). The new
  table appears on the next `init_db`; existing rows are untouched.

- Add two writers to `db.py`, beside the existing upserts, reusing the
  connection/transaction helpers (no new connection or transaction code):

  - `upsert_interpretation(conn, run_date, scope, text, model, now)`: the writer
    the dashboard plan deferred to "Phase 4 (for the external generator)". Insert
    on `(run_date, scope)`, on conflict overwrite `text`, `model`,
    `generated_at`. Does not commit (caller wraps in `transaction`). Mirrors the
    existing upsert idiom but with the composite-PK shape, not `_build_upsert_sql`
    (that helper is shaped for single-key API/user-column tables).

  - `log_invocation(conn, run_date, scope, model, temperature, seed, filter,
    input_tokens, output_tokens, now)`: plain INSERT into `llm_invocations`.
    Does not commit (caller wraps). `filter` is NULL in v1.

- Add a `[prices]` section to `settings.toml` and wire it through `config.py`:
  - Extend `SETTINGS_DEFAULTS` with a `PRICES` entry (a dict keyed by the
    canonical `provider:model` string, each value `{input, output}` in USD per
    1M tokens).
  - TOML KEY QUOTING IS MANDATORY. The keys contain `:` and `.`, both of which
    break a bare TOML key (a dot is a table separator; a colon is invalid), so
    every key MUST be quoted. Seed today's budget-tier numbers (verified
    2026-06-09):

    ```toml
    [prices]
    "anthropic:claude-haiku-4-5"   = { input = 1.00, output = 5.00 }
    "openai:gpt-5.4-nano"          = { input = 0.20, output = 1.25 }
    "xai:grok-4-fast"              = { input = 0.20, output = 0.50 }
    "google:gemini-2.5-flash-lite" = { input = 0.10, output = 0.40 }
    ```

    These are estimates and will drift; the comment records the verify date.
  - Extend `validate_config` to check `PRICES` is a dict and every entry has
    numeric, non-negative `input` and `output`. A malformed hand-edit fails loud
    at import, consistent with the existing externalized-config validation.

Files touched: `db.py` (schema statement + version bump + two writers),
`config.py` (`SETTINGS_DEFAULTS` + validation), `settings.toml` (`[prices]`).

### Test cases (mocked/offline; mirrors `testing/test_db.py`)

- Fresh DB: `init_db` creates `llm_invocations` with the temperature/seed/filter
  columns and stamps `user_version = 4`.
- v3->v4 migration is non-destructive: construct a real v3 schema (including the
  existing `interpretations` and `categories` tables, not stripped stand-ins)
  with `user_version = 3` and a sample `videos` row plus a sample
  `interpretations` row; run `init_db`; assert `llm_invocations` now exists,
  `user_version = 4`, and BOTH pre-existing rows are byte-for-byte unchanged.
- `upsert_interpretation` inserts a row, then a second call on the same
  `(run_date, scope)` overwrites `text`/`model`/`generated_at` and leaves a row
  with a different scope untouched.
- `log_invocation` appends and records parameters: two calls for the same
  `(run_date, scope)` yield two rows with distinct ids (proves it is a log); the
  written `temperature`, `seed`, and `filter` round-trip, and a NULL seed and
  NULL filter store as NULL.
- Update the existing `testing/test_db.py` table/column inventory: add
  `llm_invocations` (all columns) and change the version assertion to `== 4`.
  (These break on the bump unless updated; the fix is in-scope here.)
- `config` price validation: a well-formed `[prices]` loads; a non-dict entry, a
  missing `output`, and a negative price each raise `ConfigError` naming the key.
- TOML parse guard: a small test loads the seeded `settings.toml` and asserts the
  four price keys are present (catches an accidental unquoted key regressing the
  file).

### Manual validation

1. Back up the seed first: `sqlite3 data/database/swipefile.db "VACUUM INTO
   'swipefile-backup-pre-v4.db'"`, then on the backup confirm `PRAGMA
   user_version;` and `PRAGMA integrity_check;`.
2. Run the suite: `pytest testing/ -q`. All green, including the updated
   inventory test asserting v4.
3. Point `init_db` at the real (backed-up) DB, then
   `sqlite3 swipefile.db ".schema llm_invocations"` shows the new table with
   temperature/seed/filter, and `PRAGMA user_version;` returns 4. Spot-check an
   existing `rankings` row is unchanged.
4. Temporarily unquote a `[prices]` key in `settings.toml`, import `config`,
   confirm the TOML parse fails loudly; restore the quotes. Then put a bad value
   (string price) and confirm a named `ConfigError`; revert.

---

## Phase 1: provider seam + adapters + cost helper

Goal: a single function that turns (provider, model, prompt, temperature, seed)
into (text, token usage), for all four providers, applying only the parameters
each provider supports, plus the display-time cost helper. No DB, no
prompt-building, no dashboard.

### Scope

- New module (e.g. `llm.py`) exposing
  `generate(provider, model, prompt, *, temperature, seed) -> GenerateResult`,
  where the result carries `text`, `input_tokens`, `output_tokens`, and the
  `seed_applied` (the seed actually sent, or None). One thin adapter per provider
  behind a dispatch on `provider`; each adapter reads its own key via `os.getenv`
  (e.g. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`, `GEMINI_API_KEY`)
  after `load_dotenv()`, and raises a clear, provider-named error if the key is
  absent.
- `split_model(s) -> (provider, model)` helper, the ONE place the canonical
  `provider:model` string is split. `generate` is the only caller.
- Per-provider parameter handling lives in the adapters:
  - Each adapter validates `temperature` against its provider's range
    (Anthropic 0-1; others 0-2) and raises a clear error if out of range.
  - The Anthropic adapter omits `seed`; the others pass it. Each adapter reports
    back `seed_applied` so the caller logs the truth (None for Anthropic).
- Each adapter pulls token counts from that provider's usage field. Adapters
  return the same `GenerateResult` shape so callers never branch on provider.
- An unknown `provider` raises a clear error listing the supported set.
- Cost helper `estimate_cost(model, input_tokens, output_tokens, prices) ->
  float | None`: looks up the canonical combined `model` string in `[prices]`,
  returns the computed USD estimate, or `None` when the model is absent (the
  NULL/"unavailable" path). Pure function, no I/O.

Files touched: `llm.py` (new), tests under `testing/`.

### Test cases

- Mocked (default suite): with the network call patched, `generate()` returns a
  populated `GenerateResult` for each provider; a missing key raises the named
  error; an unknown provider raises the supported-set error.
- `split_model`: `"anthropic:claude-haiku-4-5"` -> `("anthropic",
  "claude-haiku-4-5")`; a model containing no colon raises a clear error.
- Parameter handling (mocked, asserting on the kwargs passed to the patched SDK
  call): `temperature` is forwarded for every provider; `seed` is forwarded for
  OpenAI/xAI/Gemini and ABSENT from the Anthropic call; `seed_applied` is the
  seed for those three and None for Anthropic.
- Temperature range: `1.5` to the Anthropic adapter raises a clear, provider-named
  error; `1.5` to OpenAI is accepted.
- `estimate_cost`: known model returns the right product
  (`input/1e6*in_price + output/1e6*out_price`); unknown model returns `None`;
  zero tokens returns 0.0.
- Live smoke (`@pytest.mark.live`, excluded by default): for EACH provider, send
  a trivial prompt ("Reply with the single word OK") capped to a few output
  tokens, assert a non-empty `text` and `input_tokens > 0`. HARD-FAIL with a
  provider-named message if the key is missing or the call errors (no skip).

NOTE (verification, not a plan defect): the exact callable API model IDs per SDK
(the strings the adapters pass) are confirmed against each provider's docs as the
first step of building this phase. The `[prices]` keys must match those confirmed
IDs.

### Manual validation

1. `pytest testing/ -q` (default, mocked): green, no network touched.
2. `pytest -m live -q`: with all four keys in `.env`, four real calls succeed.
   Temporarily rename one key, re-run, confirm that provider's test FAILS with a
   clear "missing KEY for <provider>" message (not a skip); restore the key.
3. In a REPL: `generate("anthropic", "claude-haiku-4-5", "Reply OK",
   temperature=1.5, seed=42)` raises the range error; `temperature=0.5` works and
   `seed_applied` is None. `generate("openai", ...)` with `seed=42` returns
   `seed_applied == 42`.
4. `estimate_cost("openai:gpt-5.4-nano", 3000, 200, prices)` returns a sane
   sub-cent number; `estimate_cost("made:up", ...)` returns `None`.

---

## Phase 2: generator core (importable module) + CLI

Goal: the orchestration that reads a lane, builds a prompt, calls `generate()`,
and persists + logs with parameters. Importable by both the CLI and (Phase 3) the
dashboard endpoint, so the button is a thin wrapper, not a reimplementation.

### Scope

- New module (e.g. `interpret.py`):
  - `build_prompt(run_date, scope, rows) -> str`: assembles the prompt from a
    lane's ranked rows (reuse `db.fetch_lane(conn, run_date, scope)`). The prompt
    states the scope and the EXACT row count so the model cannot overstate a
    trend from a thin set, and asks for 3-4 sentences. `fetch_lane` LEFT JOINs
    `videos`, so a ranking whose video row is missing yields NULL title/channel/
    views; `build_prompt` must tolerate NULLs (omit or label the field, never emit
    the literal "None"). Single source of truth for prompt text.
  - `synthesize_lane(conn, run_date, scope, model, *, temperature, seed,
    filter=None) -> result`: `model` is the canonical combined string. Fetch the
    lane; if empty, return a "skipped, no row" sentinel and do NOT call the LLM.
    Otherwise call `generate()` (which splits the model and applies params), then
    in ONE `run_with_db_retry(lambda: ...)` wrapping a `transaction`, perform the
    writes IN THIS PINNED ORDER: (1) `log_invocation` with the params and
    `seed_applied` from the result and `filter` (NULL in v1), (2)
    `upsert_interpretation`. The order is pinned so the atomicity test (below) is
    meaningful. Network happens before the transaction opens.
  - `synthesize_run(conn, run_date, model, *, temperature, seed)`: loop the three
    scopes sequentially, calling `synthesize_lane` for each; collect per-scope
    outcomes (written / skipped-empty). One model + parameter set for the run.
- CLI entry (`python interpret.py`): argparse with `--run-date` (default: latest
  from `fetch_run_dates`), `--model provider:model` (the canonical string),
  `--temperature` (default a sane value, e.g. 1.0, validated downstream by the
  adapter), `--seed` (optional int), optional `--scope` to do just one lane
  (default: all three). Loads `.env`, validates config, `init_db`, runs
  `synthesize_run`, prints a per-scope summary to stderr in the existing logging
  style.

Files touched: `interpret.py` (new), tests under `testing/`.

### Test cases

- `build_prompt`: includes the scope, the literal row count, and per-row fields;
  a 1-row lane produces a prompt whose count says 1; a row with NULL video fields
  does not emit "None" (NULL-tolerance regression guard).
- `synthesize_lane` with `generate` mocked: a non-empty lane writes exactly one
  `interpretations` row for `(run_date, scope)` with `model` = the canonical
  string, and appends one `llm_invocations` row carrying the temperature, the
  `seed_applied`, NULL filter, and the mocked token counts.
- Empty lane: `synthesize_lane` does NOT call the mock and writes NO
  interpretations row and NO invocation row.
- Re-run overwrite: calling `synthesize_lane` twice for the same `(run_date,
  scope)` leaves ONE interpretations row (overwritten) and TWO invocation rows.
- Transaction atomicity (relies on the pinned write order): patch
  `upsert_interpretation` to raise. Because `log_invocation` ran first inside the
  same transaction, assert the rollback removed the invocation row too (no orphan
  log), proving the single-transaction wrapping.
- `synthesize_run`: three scopes, mixed (one empty), produces the right
  written/skipped set and uses one model + parameter set across all calls.
- CLI: default run-date resolves to the latest in `rankings`; `--scope` limits to
  one lane; `--seed` reaches the invocation row (for a seed-supporting provider).

### Manual validation

1. `pytest testing/ -q`: green.
2. Against the backed-up real DB:
   `python interpret.py --model anthropic:claude-haiku-4-5 --temperature 0.5`
   (no `--run-date`). Confirm stderr reports three scopes, and
   `sqlite3 swipefile.db "SELECT scope, model, length(text) FROM interpretations
   WHERE run_date = (SELECT MAX(run_date) FROM rankings)"` shows the rows.
3. `sqlite3 swipefile.db "SELECT scope, model, temperature, seed, filter,
   input_tokens, output_tokens FROM llm_invocations ORDER BY id DESC LIMIT 3"`:
   temperature is 0.5, seed is NULL (Anthropic), filter is NULL.
4. Repeat with `--model openai:gpt-5.4-nano --seed 42`; confirm the new invocation
   rows show seed 42.
5. Re-run; interpretations still has one row per scope (overwritten),
   llm_invocations grew. Read one summary by eye: 3-4 sentences, no overstated
   trend from a ~20-row snapshot.

---

## Phase 3: dashboard run button + model/parameter controls + stopwatch

Goal: trigger synthesis from the dashboard for the lane being viewed, with a
user-chosen model and parameters and a visible timer.

### Blocking contract change (explicit, not silent)

The dashboard plan declares interpretations READ-ONLY in three places (Phase 0
"read-only-strict: the app has no write path at all"; `upsert_interpretation`
deferred because "the DASHBOARD never calls it"; the generator listed out of
scope for v1 with "the dashboard only displays its output"). A run button gives
the dashboard a write path to `interpretations` and `llm_invocations` plus an
outbound LLM call, which contradicts all three. This phase AMENDS
`dashboard-plan.MD` on purpose to permit exactly one interpretation-write
endpoint (the run button) and the spend read endpoint (Phase 4). The amendment is
a tracked edit to that doc, called out here so the contract change is deliberate.
Everything else in the dashboard stays read-only.

### Scope

- One write endpoint, e.g. `POST /api/interpret` with body `{run_date, scope,
  model, temperature, seed}` (model is the canonical `provider:model`; seed
  optional/nullable). It calls `interpret.synthesize_lane` for that single lane
  and returns `{text, input_tokens, output_tokens, seed_applied, skipped}`.
  Persists the canonical row and logs the invocation via the Phase 2 core. Keys
  never leave the server; the browser sends only the model + parameter choices.
- Controls: a model dropdown of the supported canonical `provider:model` options,
  a temperature input, and an optional seed input. On page load, prepopulate ALL
  THREE from the most recent `llm_invocations` row by `MAX(id)` (single read); if
  the table is empty, default to the first model option, a default temperature,
  and empty seed. The seed input may be disabled/greyed when an Anthropic model
  is selected, since it has no effect there.
- Run button: fires the single POST for the current lane. On a provider/key/range
  error the endpoint returns a clean error payload and the UI shows an inline
  message (e.g. "OpenAI key not configured", "temperature out of range for
  Anthropic"), never a raw 500.
- Stopwatch (client-side): start a timer on button click, tick the display
  (~100ms), freeze it when the response (the summary) lands, show final elapsed.
  One lane per click, one timer.

Files touched: `dashboard/app.py` (one write endpoint + prepopulation read),
`dashboard/static/` (controls, button, stopwatch JS), `dashboard-plan.MD`
(amendment), tests under the dashboard suite.

### Test cases (TestClient, `synthesize_lane`/`generate` mocked)

- `POST /api/interpret` for a non-empty lane persists one interpretations row and
  logs one invocation carrying the posted temperature/seed; response has text +
  token counts + `seed_applied`.
- Empty lane: response indicates `skipped`, no row written, no invocation logged.
- An out-of-range temperature for the chosen provider, and a missing key, each
  yield a clean error status + message body, not an unhandled 500.
- Prepopulation read returns the most recent `llm_invocations` row's model,
  temperature, and seed; with an empty table it returns a null/empty the client
  maps to defaults.
- The dashboard's existing read endpoints remain unchanged and still pass.

### Manual validation

1. Dashboard suite green.
2. Start the dashboard, open a run_date, view the `health` lane. The controls show
   the last-used model, temperature, and seed (or defaults on a fresh DB).
3. Click Run: the stopwatch ticks, freezes when the summary appears, and the text
   renders. Reload; the stored summary loads via the existing read path and the
   controls prepopulate from the invocation just logged.
4. Select an Anthropic model: confirm the seed input is disabled/inert. Select an
   OpenAI model, set seed, Run, and confirm the logged row shows that seed.
5. Set temperature 1.8 with an Anthropic model selected, Run: an inline range
   error, no 500. Rename a provider key, pick it, Run: an inline key error.
6. Switch to a lane with no ranked rows, Run: clean empty/skip state, no new rows.

---

## Phase 4: spend display

Goal: show the user what their LLM runs have cost, computed at display from the
price map.

### Scope

- One read endpoint, e.g. `GET /api/spend` (optionally `?run_date=`), aggregating
  `llm_invocations`: per invocation (or grouped by run_date/model) it returns
  input/output tokens and a computed cost via `estimate_cost` against
  `settings.toml [prices]`. Rows whose model is absent from the map return cost
  `null`. Ordering uses `id` (or parsed instants), never a lexical compare on
  `generated_at`.
- A small spend panel: a compact table or summary (total estimated spend, and a
  per-model breakdown), every dollar figure labeled "est.", and "unavailable"
  wherever cost is null. Tokens are always shown (exact).

Files touched: `dashboard/app.py` (one read endpoint), `dashboard/static/`
(spend panel), tests.

### Test cases

- `GET /api/spend` against seeded `llm_invocations`: correct token totals and
  cost computed from a known price map; an invocation with an unpriced model
  returns cost `null` while still reporting tokens.
- Recompute-on-price-change: changing `[prices]` changes the returned cost for the
  same stored rows (proves cost is computed, not stored).
- Empty log: endpoint returns an empty/zeroed payload, not an error.

### Manual validation

1. Tests green.
2. After a couple of real runs, open the spend panel: token counts match
   `sqlite3 swipefile.db "SELECT input_tokens, output_tokens FROM
   llm_invocations"`, est. cost is a believable sub-cent-to-cents figure.
3. Edit one price in `settings.toml`, reload, confirm the est. cost changed with
   NO DB migration and NO row rewrite.
4. A model not in `[prices]` shows tokens but "unavailable" for cost.

---

## Parked to v2 (explicitly out of scope here)

- Rail-filtered interpretations ("summarize what I'm looking at"). v1 always
  synthesizes the full lane; the `llm_invocations.filter` column exists now so a
  v2 filtered run can be logged without a schema bump. (Whether a filtered
  summary also PERSISTS to `interpretations`, and under what key, is the v2
  storage question, still open.)
- Change-over-time inputs to the prompt (cross-run trend from `stats_snapshots`
  and prior `run_date`s).
- The Cowork scheduled run (task 1 = YouTube fetch, task 2 = synthesis). v1 is
  the dashboard button (and the Phase 2 CLI).
- Per-lane parallel synthesis and three independent timers (v1 is sequential).
- An automated price-refresh helper that rewrites `[prices]`.

## Reuse map (do not rebuild)

- `db.get_connection` (WAL), `db.transaction`, `db.run_with_db_retry` for all
  writes (retry outer, transaction inner).
- `db.fetch_run_dates` (latest run_date for the default target), `db.fetch_lane`
  (lane rows for the prompt), `db.fetch_interpretation` (dashboard read, exists).
- `config.now_local_iso()` for the `generated_at` timestamps ONLY. The generator
  does NOT derive `run_date` from `now`; `run_date` comes from `rankings` (the
  run the pipeline already wrote). Never equate `run_date` with
  `config.pacific_date` (quota-ledger only).
- `config.load_settings` / `SETTINGS_DEFAULTS` / `validate_config` for the
  `[prices]` map; `load_dotenv()` + `os.getenv` for keys.
- The existing v2->v3 `IF NOT EXISTS` + `user_version` migration path for the
  v3->v4 bump (no new migration branch).
- 