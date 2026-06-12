Plan: model + price registry with in-app editing
Objective
Replace the hardcoded model list and settings.toml [prices] with two DB tables that are the single source of truth for which models exist, which appear in the dropdown, each model's parameter capabilities, and effective-dated prices that sum correctly across mid-month price changes. Add an in-app /models page (prebuilt grid) to edit them. This subsumes the outstanding xAI and Anthropic stale-string fixes as data.
Standing rules
Plan-before-execute, one phase, explicit STOP gate, CC brings the diff. No em dashes. No magic numbers. Code wins over docs; read source before asserting. Verify model strings against the live provider API, never from memory or a docs summary. WAL safety: any step touching live swipefile.db takes a verified VACUUM INTO backup first and smokes against a copy via DASHBOARD_DB_PATH, never the live seed. All writes through db.run_with_db_retry (outer) + db.transaction (inner). No auth (local single-user tool).
Schema (locked)
models (attributes, one row per model):

model TEXT PRIMARY KEY, canonical provider:model
provider TEXT
enabled INTEGER DEFAULT 1, dropdown flag (hide without deleting)
supports_temperature INTEGER, pass temperature only if 1
supports_seed INTEGER, pass seed only if 1
is_reasoning INTEGER, informational/UI
deleted INTEGER DEFAULT 0, soft-delete flag
added_at TEXT, notes TEXT NULL

model_prices (dated windows, append-only data):

id INTEGER PRIMARY KEY AUTOINCREMENT
model TEXT (joins models.model)
input_per_1m REAL, output_per_1m REAL
valid_from TEXT (inclusive, YYYY-MM-DD)
valid_to TEXT NULL (exclusive; NULL = current)
deleted INTEGER DEFAULT 0, soft-delete flag
recorded_at TEXT

No foreign keys. No provenance flag.
interpretations gains: temperature REAL NULL, seed INTEGER NULL (applied values, NULL when the provider omitted them).
Cross-cutting rules

deleted and enabled hide from forward use (dropdown, new-run validation) but NEVER from backward pricing (spend). The spend effective-price lookup ignores both flags; it prices any invocation against whatever window covers its generated_at, deleted or not. This is what makes soft delete safe.
Pricing is per-invocation, then summed. For each llm_invocations row, find the model_prices window where valid_from <= generated_at AND (valid_to IS NULL OR generated_at < valid_to), compute that row's cost, then aggregate. Additive across mid-month changes. Compute-at-display; price is derived, never stored.
Price windows for a model must not overlap. Enforced in code by the add-price endpoint auto-closing the prior open window.
Capability flags are verified against the provider when a model is added, not assumed. A wrong flag fails at generation, surfaced inline once Fix A lands.


Phase 0: discovery + dead-config cleanup
Goal: replace inference with fact before building; remove the dead .env line.
Scope:

CC reads the code and reports, with file/line cites, where the model list, dropdown options, allowlist/validation, prices, and the capability check (_is_openai_reasoning_model or equivalent) live today.
Confirm the Anthropic string the code actually uses: sqlite3 <copy> "SELECT model FROM llm_invocations WHERE model LIKE 'anthropic:%' ORDER BY id DESC LIMIT 1". This is the cross-check for the seed.
Confirm nothing reads the .env haiku model var (grep getenv near model/haiku and the var key). If dead, delete from .env and .env.example. If something reads it, STOP and report (changes the plan).
Report current settings.toml [prices] verbatim (the seed source).

Output: findings note + the .env deletion. No table work.
STOP.

Phase 1: tables, derivation, seed, stale-string fix
Goal: tables exist, everything derives from them, dropdown/pricing/capabilities read from them, stale haiku/grok strings corrected as seed data. After this phase the hardcoded list and [prices] are gone as sources.
Step 0, fold in Fix A (clean provider errors): wrap each adapter's SDK call so a provider API error becomes LLMError(f"{provider}: {message}"), catching the verified SDK base class per the prior approved plan (openai 2.41.0 OpenAIError, anthropic 0.109.1 AnthropicError, google genai.errors.APIError), confirmed against installed versions. This lands first because hand-editing models makes bad-string and bad-flag mistakes likely and you want them inline, not as a 500.
Steps:

Schema bump (additive). Add both tables via CREATE TABLE IF NOT EXISTS; add the two interpretations columns. Same non-destructive init_db path. Update testing/test_db.py inventory (both tables, all columns, the new interpretation columns) and the version assertion.
db.py readers (reuse get_connection): fetch_dropdown_models (enabled=1, deleted=0, has a current price), fetch_model (attributes for one model), fetch_effective_price(conn, model, on_date), and the spend join.
fetch_spend rewrite: price per invocation against its effective window, then sum. Ignores deleted/enabled. Add a test with two windows for one model in one month asserting the additive total.
Capabilities from flags: adapters read supports_temperature / supports_seed from the model row; pass each parameter only when set. Delete the name-pattern reasoning check. Provider-level rules (temp range, Anthropic no-seed) stay in adapters.
Dropdown + validation from the table: options endpoint reads fetch_dropdown_models; _allowed_models reads the table. Delete the hardcoded list.
Seed once (insert-if-empty, ON CONFLICT DO NOTHING) from the Phase-0 [prices]: one models row per model (enabled=1, deleted=0, flags from live capability logic, provider via split_model); one open model_prices row per model (valid_from = today, valid_to NULL). Correct the stale strings here, live-verified: xAI (GET live /v1/models, cheapest suitable grok, one real generation, then write the exact string + its current price); Anthropic (claude-haiku-4-5 confirmed against the live models list, cross-checked against the Phase-0 logged value, priced $1/$5). Retire [prices] as the live source.
upsert_interpretation writes the applied temperature/seed (NULL when omitted); test the overwrite on re-run.

Files: db.py, config.py, adapters, options/validation endpoints, seed routine (new), settings.toml (retired/kept as seed literal), tests.
STOP.


Phase 2: /models editing page
Goal: edit the two tables in-app with a prebuilt grid.
Stack: Tabulator (vanilla JS, CDN script tag, no build step), styled to the Glass Theme. A new /models route via the existing router; FastAPI serves it like the other routed pages.
models grid: fully editable inline, PK (model) read-only. Edit enabled, the three flags, notes. Add row (new model: string + flags). Soft-delete (warned, with affected-invocation count). Restore. Optional show-deleted toggle.
model_prices grid: existing rows read-only (immutable data). Add-price action = insert new window; the endpoint auto-closes the prior open window (valid_to = new valid_from) in the same transaction. Soft-delete (warned + count). Restore. Show-deleted toggle.
Endpoints (all wrapped, server-validated, no auth):

models: read (optional include-deleted), update (PK-locked, validate flags are 0/1, model string non-empty), insert, soft-delete, restore.
model_prices: read (optional include-deleted), insert (new window + auto-close prior; validate non-negative prices, valid date, no resulting overlap), soft-delete, restore. No data update.
A small count read for the delete warning: invocations referencing a model / falling in a window.

Delete warning: consequence-specific with the real count, e.g. "This model has N logged runs. Deleting hides it from the dropdown; past runs stay priced." Soft delete, so the message is honest that history is preserved and the action is reversible.
Server validation (not just cell-level): non-negative prices, valid provider:model shape, flags 0/1, dates well-formed, add-price produces no overlap. Soft-delete/restore is a single controlled flag flip through its own endpoint (the only write allowed to touch an existing model_prices row; data cells stay immutable).
Tests: each endpoint (insert, edit, soft-delete hides from dropdown but not from spend, restore, add-price auto-closes prior window, overlap rejected, negative price rejected, PK edit rejected); the count read; existing read endpoints unchanged.
Manual validation (browser, DB copy via DASHBOARD_DB_PATH): add a model, see it in the dropdown; soft-delete it, gone from dropdown but its past runs still price in the spend panel; add a price mid-month, confirm the prior window auto-closed and the spend total reflects both windows additively; delete warning shows the right count; restore brings a row back.
STOP.

Dependencies

Fix A is folded into Phase 1 step 0 (was a separately approved plan; it lands here because hand-editing needs inline errors).
Phase 2 depends on Phase 1 (edits tables that Phase 1 creates).
The earlier standalone xAI/Anthropic stale-string plan is fully subsumed by Phase 1 step 6 and retires.