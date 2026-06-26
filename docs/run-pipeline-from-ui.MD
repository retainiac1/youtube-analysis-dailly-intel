# CC Build Plan: YT Extraction page (daily-intel)

For Claude Code. Plan-before-execute. STOP after every phase and wait for review.

## Decisions baked in (veto any before CC starts)

1. **Model picker is in.** The discover run calls the LLM classifier (kid-video
   removal plus language validation), so the left card has a **Primary Model** and
   a **Fallback Model** select. A keyword title match runs before the LLM as a
   cheap pre-filter; that is a swipefile-internal change, not part of this page.
   Temperature / seed / think are NOT exposed: the classifier runs deterministic.
2. **Spend panel kept and reused.** It now genuinely reflects this run's classifier
   LLM spend, reused exactly like Interpretation / Models, zero duplication.
3. **Modes exposed:** primary "Run discovery" (`--discover`) plus secondary "Dry
   run / preview quota" (`--dry-run`, free, no writes). `--refresh` is the
   scheduled path and is not exposed here. Veto to add it.
4. **Status pill is generic.** It maps `code` plus `reason` through one table with
   an unknown-reason neutral fallback. All eight reasons are live, including
   `classify_budget_exceeded` (code 8, the per-run fallback-spend breaker).

## Phase 0: blocking confirm (model-picker wiring)

The classifier resolves its models through `active_classification_model()` and
`active_classification_fallback_model()`, NOT through `app_preferences.extraction_model`
(that key belongs to `price_refresh`). Before Phase 4, confirm where those two
functions read from:

- **If they already read `app_preferences` keys**, the picker just writes those
  keys: page-only change, no swipefile edit.
- **If they read `settings.toml` / a config constant**, making the models
  user-selectable requires the resolver to read `app_preferences` first. That is a
  swipefile/classify change and a dependency outside this page.

Send the resolver definition (and any keys/defaults it reads) so this is pinned
before CC wires the dropdowns. Do not assume `extraction_model`.

Also confirm the dashboard's existing test setup (harness and location, e.g.
pytest under `tests/`, and any JS test harness) so CC extends it rather than
introducing a new one. See Testing below.

## Non-negotiables (house rules, enforce throughout)

- Call `scripts/run-pipeline.sh`, NEVER `swipefile.py` directly. The wrapper runs
  the WAL-safe pre-run backup and aborts if it fails; bypassing it means a run
  with no restore point.
- Reuse the spend panel with zero duplication. Do not copy, fork, or re-implement
  `spend.js` or `/api/spend`.
- Use the **frontend-design skill**, but constrain output to the existing Glass
  Theme: reuse the existing CSS variables and the `.glass-panel` / `.page-aside`
  classes, add no new color tokens, no CDN, vendor any library offline.
- No em dashes anywhere (code, comments, UI copy).
- No magic numbers. SSE keep-alive interval, retained-line cap, lock timeout, and
  any other constant live in config/settings, not inline.
- Code wins over docs; cite `file:line` when they disagree.
- Verify in a real browser before claiming a phase done. No assumptions.
- Tests stub the subprocess. No test invokes the real `run-pipeline.sh` or the
  YouTube API; that burns quota, is nondeterministic, and is slow.

## Testing

- Extend the dashboard's existing suite (confirmed in Phase 0); do not introduce a
  new framework. Add tests there, following its conventions.
- Stub the subprocess: feed canned stderr (including a `PIPELINE_RESULT` line) to a
  fake process. Never shell out to the real pipeline in a test.
- Design for testability: extract the stderr-line-to-event mapping and the
  `PIPELINE_RESULT` parse as pure helpers, not buried in the async generator, so
  they unit-test without a running server.
- Each phase's STOP gate includes its tests passing.

## Reuse map (exact pointers, do not reinvent)

- SPA serve (index.html for all routes): `dashboard/app.py:1102`
- Spend data route: `GET /api/spend?run_date=` at `dashboard/app.py:592`
- Spend renderer factory: `dashboard/static/js/spend.js:240` (`create(mount)`)
- Spend mount markup pattern: `dashboard/static/index.html:137`
  (`aside.glass-panel.spend-panel` inside the `.page-aside` shell at :135 to :138)
- Spend wiring: `dashboard/static/js/main.js:382` (create); refresh on page entry
  `:210`, on run-change `:427`, on theme change `:246`
- Model dropdown source: `db.py:882` (`fetch_dropdown_models`); the Interpretation
  Model select reads the same source, mirror it
- Preference read/write: `db.py:1242` (`get_preference`), `db.py:1252`
  (`set_preference`); reuse the existing preference-write HTTP route if one exists
  (Interpretation/Models persist selections somewhere), else add a minimal one
- Classifier model resolver: `active_classification_model()` /
  `active_classification_fallback_model()` (location to confirm in Phase 0)
- Page structural template: the **Interpretation** page section in
  `dashboard/static/index.html`. Locate it and mirror its `.page` / `.page-aside`
  shell; it is the page this one should resemble.
- Pipeline entry: `scripts/run-pipeline.sh` (cd repo root, `.venv` check, pre-run
  backup, exec `swipefile.py`, passthrough args)
- Exit contract: `swipefile.py` `_finalize` prints one line
  `PIPELINE_RESULT: code=N reason=slug` to **stderr**; codes and slugs live in
  `config.py:387` to `:395` plus `swipefile._EXIT_REASON`. All human-readable
  output is on stderr; stdout is empty.

## Phases

### Phase 1: backend run/stream endpoint

Add to `dashboard/app.py`, following the existing route conventions:

- One start-and-stream SSE endpoint, e.g. `GET /api/extract/stream?mode=discover`
  (EventSource forces GET; `mode` is `discover` or `dry-run`, validated against an
  allow-list).
- **Single-flight lock:** a module-level `asyncio.Lock`. If already held, return
  409 and start nothing. Rationale: overlapping runs race `state.json` and the WAL
  lock, which is exactly how `db_locked` is manufactured.
- Spawn `scripts/run-pipeline.sh` with the mode flag via
  `asyncio.create_subprocess_exec`, cwd = repo root, `stdout=DEVNULL`,
  `stderr=PIPE`. The run reads its classifier models from preferences itself; the
  endpoint passes no model args.
- Read stderr line by line; emit each as an SSE `log` event. On the
  `PIPELINE_RESULT` line, parse `code` and `reason` and emit a terminal `result`
  event, then close the stream and release the lock.
- Also fold the child process exit code into the `result` event as a defense
  against a missing `PIPELINE_RESULT` line (treat absence as `code=5/other`).
- All constants (keep-alive interval, read settings) in config, not inline.

Test with curl: a discover stream, a dry-run stream, and a 409 while a run is in
flight.

Tests (stubbed subprocess): the `PIPELINE_RESULT` parser over all eight reason
slugs plus a missing line (falls back to other/5) and a malformed line; the mode
allow-list rejects unknown modes; a concurrent start returns 409; canned stderr
produces ordered `log` events then one terminal `result` event. **STOP.**

### Phase 2: page shell and nav (frontend-design skill)

- Read the frontend-design skill first.
- Add the nav tab (e.g. "Extract") to the nav in `index.html` and register the
  route in `main.js`, mirroring how Interpretation registers.
- Build the page by mirroring the Interpretation markup: `.page` plus
  `.page-aside` shell, `.glass-panel` cards.
  - Left card "Run extraction": Primary Model select, Fallback Model select,
    primary button "Run discovery", secondary "Dry run (preview quota)", a
    run-time readout (mirror Interpretation's "Run time"), and a status-pill slot.
    No temp / seed / think controls.
  - Center: an empty monospace terminal placeholder filling the area Interpretation
    uses for generated text.
  - Right `.page-aside`: a spend mount `aside` with a NEW id (e.g.
    `#extract-spend-panel`), same classes as `index.html:137`.
- Reuse existing CSS variables and classes only. **STOP.**

### Phase 3: wire streaming, terminal, and pill

- On Run click: open an `EventSource` to the Phase 1 endpoint with the chosen
  mode; disable both run buttons while in flight; start the run-time ticking.
- Append each `log` event as a line in the auto-scrolling monospace terminal.
  Escape text. Cap retained lines via a config constant to bound memory.
- On the `result` event: stop the timer, render the status pill from a single
  `code` to `{label, severity}` map with an unknown-reason neutral fallback,
  re-enable the buttons.
- On 409 or connection error: show a clear "a run is already in progress" state and
  do not open a second `EventSource`.
- Tests: if a JS harness exists, unit-test the pill `code` to `{label, severity}`
  map over all reasons plus the unknown fallback; otherwise browser-verify. **STOP.**

### Phase 4: model selects (after Phase 0 confirms the source)

- Populate Primary and Fallback selects from `fetch_dropdown_models` (`db.py:882`),
  same source as the Interpretation Model select.
- Persist the choices to the classifier's primary/fallback preference keys
  (confirmed in Phase 0) via `set_preference` (`db.py:1252`), reusing the existing
  preference-write route or a minimal new one. Pick writes the pref; Run reads it;
  persistence makes it the default for later runs.
- The picker selects the two model identities only. The sticky-on-429 failover,
  the per-run fallback-spend breaker, and the exit-code contract stay
  swipefile-internal and unchanged.
- Tests: a set/get round-trip writes the confirmed classifier keys (not
  `extraction_model`); the selects populate from `fetch_dropdown_models`. **STOP.**

### Phase 5: reuse the spend panel (zero duplication)

- In `main.js`, after the page mounts, call
  `spend.create(document.getElementById('extract-spend-panel'))` exactly as
  `pricesSpend` does at `:382`.
- Wire the same three refresh triggers as Prices: page entry (`:210` pattern),
  run-date change (`:427` pattern), theme change (`:246` pattern).
- Reuse `GET /api/spend?run_date=` unchanged. Do not touch `spend.js`.
- Verify in browser: the panel renders identically to Prices / Interpretation and
  updates on run-date and theme change. **STOP.**

## Out of scope (do not touch)

- The keyword title pre-filter and the LLM classifier internals (swipefile-side
  changes); this page only consumes their contract.
- Internals of `swipefile.py`, `config.py`, `run-pipeline.sh`.
- Internals of the spend panel (`spend.js`, `/api/spend`).

## Phase 6: live verification (merged-discover run via the Extract page)

The single real merged-discover triggered through the Extract page "Run discovery"
button (not the CLI), proving the v12/v13 catalog sweep, lifecycle transitions, the
once-per-Pacific-day cap, and `run_summary` all work end to end against the live
catalog. Also discharges the deferred Phase 7 manual check. Verified 2026-06-26; both
presses passed.

Inspection rule (load-bearing): a bare `immutable=1` read of the live
`data/database/swipefile.db` is STALE, it ignores the `-wal` sidecar where the run's
writes live until checkpoint (it reported `user_version=11` while the real state was
v13). Every post-run assertion folds a WAL-safe snapshot first
(`sqlite3 data/database/swipefile.db "VACUUM INTO 'PR.db'"`) and reads that via
`immutable=1`. Never `wal_checkpoint` (it writes) when only reads are authorized.

Pre-run gate (all confirmed before pressing): the UI runner spawns
`scripts/run-pipeline.sh` (`dashboard/extract.py:104-107`), which writes a WAL-safe
pre-run backup via `backup_database.py` (`scripts/run-pipeline.sh:57`) before
`swipefile.py` runs; a fresh `swipefile.db.bak-20260626T145207Z` appeared ahead of the
run's writes. Dry-run is read-only and never migrates (`db.get_readonly_connection`,
`swipefile.py:1792-1820`). Git was clean at the Phase 7 gate (`fe87c21`).

Press 1, `run_id=20`, mode `discover`: added 44, updated 392, aged_out 0, gone 5,
snapshots 436, classify cost $0.0398. All six assertions passed against the WAL-folded
snapshot:
- (a) catalog-wide: `snaps 436 == run_summary.snapshot_count 436`, reconciles
  `snaps == active_now (436) + aged_out (0)`, and `436 >> added (44)`, so the sweep
  covered the whole catalog (397 active grew to 441 at sweep time: 44 caught, 5 gone),
  not just the catch.
- (b) no duplicate snapshot per video (zero, also held by `UNIQUE(run_id, video_id)`).
- (c) the 06-08 cohort video `Wwj_6wOTvds` got a fresh row at the run instant, still
  `active`, its `last_view_growth_at` advanced to today.
- (d) rankings recomputed once: habit/health/overall each one `captured_at`, a single
  distinct value across all buckets, equal to the run instant.
- (e) `gone` fired live for the first time: 5 `videos.list`-absent ids
  (`LIMup4VPNOY`, `WhYMy_lweEs`, `1Mw9XiwIIKQ`, `aiTEfSjmKIs`, `c4IJZOj-BCU`) flipped to
  `gone` at the run instant, zero of them present in this run's snapshots.
- (f) exactly one `run_summary` row, `mode='discover'`, counts matching the pane.

Press 2, `run_id=21`, mode `refresh` (pressed after Press 1 committed and the button
relabeled to "Refresh stats"): the once-a-day cap forced refresh-only. added 0,
updated 436, aged_out 0, gone 0, snapshots 436, classify cost $0.0000, 10 quota units
(the `videos.list` sweep only, no `search.list`). `added == 0` is the structural proof
the cap prevented a second catch; the sweep stayed catalog-wide (`snaps 436 ==
active_now 436`).

Open follow-up (not fixed here): the dashboard migrates the live seed on normal
startup with no backup gate, `dashboard/app.py:64-77` `_lifespan` calls
`db.init_db(get_db_path())` against `config.DB_PATH` unless `DASHBOARD_DB_PATH` is set,
with no pre-migration backup (unlike `run-pipeline.sh`). Harmless on the current v13
seed (`init_db` is additive and idempotent, a no-op), but a future `SCHEMA_VERSION`
bump would migrate the live seed on a plain launch with no restore point. Gate the
lifespan migration behind the same backup step, or point the dashboard at a copy, in a
later phase.