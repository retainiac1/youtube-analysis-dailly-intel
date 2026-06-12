Plan: move max_tokens from a flat constant into the models table (per-model, user-editable)

Objective
Replace the single hardcoded output-token limit (a flat 512 used by every adapter) with a per-model max_tokens column in the models registry, editable in /models like the other model controls, defaulted from config for new models, and validated. The per-adapter parameter-name mapping already exists and is tested; this change only swaps the source of the value from a constant to the model row. After this, each model carries its own explicit output cap, nothing is ever uncapped, and the cap is tunable without code changes.

Why
Today the output limit is the one runtime control that is NOT per-model: temperature, seed, and reasoning are gated per row, but max_tokens is a flat constant for every model and provider (llm.py adapters at :244 anthropic, :271 openai, :296 xai, :320 gemini all use the same 512). That is wrong for two reasons: a reasoning model needs a generous cap (thinking tokens are drawn from the same budget, and too small a cap yields a paid-for empty answer) while a cheap model can run lean, and the limit should be a visible, editable per-model attribute, not a buried constant.

Standing rules
Plan-before-execute, one phase at a time, explicit STOP gate, CC brings the diff. No em dashes. No magic numbers (the flat 512 becomes a named config default; the migration backfill preserves the current value rather than re-introducing a literal). Code wins over docs; read source before asserting. WAL safety: every step touching live swipefile.db takes a verified VACUUM INTO backup first and smokes against a copy via DASHBOARD_DB_PATH, never the live seed. All writes through db.run_with_db_retry + db.transaction. Additive-first migration, version stamped LAST. The suite is green at every STOP.

Confirmed context (from the grep, carried in)
- Flat value is 512, used by all four adapters; the per-adapter param name is already correct and locked by tests: anthropic/xai emit max_tokens, openai emits max_completion_tokens, gemini emits max_output_tokens in GenerateContentConfig (test_llm.py:151).
- Provider semantics (from API research, informs the column, not the param mapping): on every provider the cap bounds total billed output INCLUDING reasoning tokens, so it is a real per-call cost ceiling. On reasoning models a too-small cap can be consumed entirely by reasoning, returning an empty answer (finish_reason length). Anthropic additionally has hard per-model output ceilings (Opus 128k, Sonnet/Haiku 64k); an over-ceiling value is rejected by the provider and surfaces through the existing Fix A wrap. We do NOT hardcode those ceilings (they drift); we validate positive + a sane upper bound and let the provider enforce its own ceiling.

Design decisions (locked)
- Column: models.max_tokens INTEGER NOT NULL. NOT NULL means a model can never be accidentally uncapped; there is never a None for an adapter to handle. The value is always explicit and visible per model.
- Default lives in config (DEFAULT_MAX_TOKENS), used only when seeding or adding a new model. It is the factory default for new rows, not a runtime fallback (there is no NULL to fall back from). This keeps a single named source for the default while every row stays explicit.
- Migration backfill = the current flat value, so behavior is identical the moment the adapters read the column (every existing row gets exactly what it has today). No behavior change at migration; the knob simply becomes editable.
- Validation: max_tokens > 0 and <= a configured sane ceiling (MAX_TOKENS_UPPER_BOUND in config, not a literal). Provider-specific ceilings are NOT encoded here; the provider rejects its own over-cap and Fix A surfaces it cleanly.
- The per-adapter param-name mapping is unchanged. Adapters keep emitting their correct parameter; only the value source changes from the constant to the passed-in per-model value.

Phase 0: confirm the single source and the call path (no code)
Goal: prove the flat constant has exactly one definition and that the generate() call site has the model row available to pass the per-model value.
Scope:
- Cite the definition of the flat 512 (a named constant or an inline literal) and EVERY read site. Confirm the four adapter uses (:244/:271/:296/:320) are the only consumers, or list any others.
- Cite the generate() signature and the call site(s) that invoke it (interpret.synthesize_lane and any other), confirming the model row (with its columns) is in scope there so max_tokens can be threaded the same way temperature/seed/think already are.
- Cite test_llm.py:151 and any other test asserting the value 512, so the value-assertion updates are known up front.
Output: findings with cites. No code.
STOP.

Phase 1: backend. Column, seam, adapters, seed (behavior-preserving)
Goal: max_tokens is a per-model column read by every adapter; behavior is identical to today because the backfill preserves the current value. The flat constant is gone.
Steps:
1. Schema bump to the next version (v9 if this lands before the surrogate-key refactor; coordinate the number with that plan if both are in flight). Add models.max_tokens INTEGER NOT NULL via the existing atomic migration block. For existing rows, backfill to the current flat value (read the constant during the migration, or write its known value once with a comment tying it to the retired constant). Add max_tokens to the CREATE TABLE models for fresh DBs with the config default. Stamp the version LAST. Update testing/test_db.py inventory (the new column) and the version assertion.
2. Config: add DEFAULT_MAX_TOKENS and MAX_TOKENS_UPPER_BOUND to settings.toml, threaded through config.py like the other settings (DEFAULT_ const -> SETTINGS_DEFAULTS -> module binding -> validate_config). DEFAULT_MAX_TOKENS equals the current flat value so existing semantics carry; it is now editable in one place.
3. Seam: generate() gains max_tokens (read from the model row by the caller, like temperature/seed/think). Thread it from the call site (synthesize_lane) into generate() and on to the adapter. The adapter param-name mapping is unchanged.
4. Adapters: replace the flat constant at each of the four sites with the passed-in max_tokens, keeping each provider's correct parameter name (anthropic/xai max_tokens, openai max_completion_tokens, gemini max_output_tokens). Remove the now-unused flat constant.
5. Seed + insert use the config default for any new model that does not specify one; the Ollama and existing seed rows get DEFAULT_MAX_TOKENS (which equals their current effective value, so no change).
6. Tests:
   - Migration: build a genuine prior-version DB with model rows, run init_db, assert max_tokens present, NOT NULL, every existing row backfilled to the preserved value, version bumped.
   - Value threading: a model row with max_tokens = N produces an adapter call carrying N under the correct per-provider parameter name (anthropic/xai max_tokens=N, openai max_completion_tokens=N, gemini max_output_tokens=N). Update the test_llm.py:151 assertions from the constant to the row value; the param-name half of those tests stays exactly as is.
   - Default: a newly seeded/inserted model with no explicit value gets DEFAULT_MAX_TOKENS.
   - Existing suite green (behavior unchanged because backfill == prior constant).
Files: db.py (schema + migration + seed), testing/test_db.py, config.py + settings.toml, llm.py (generate() seam + four adapter value swaps + remove constant), interpret.py (pass max_tokens from the row), testing/test_llm.py (value assertions).
STOP.

Phase 2: editing surface. /models column + validation
Goal: the per-model max_tokens is editable in the registry editor and validated server-side.
Steps:
1. Endpoints (dashboard/app.py): add max_tokens to ModelUpdate (PUT /api/models/{model}) and ModelInsert (POST /api/models, default DEFAULT_MAX_TOKENS). Server validation: integer, > 0, <= MAX_TOKENS_UPPER_BOUND, else 422. PK-lock and the existing insert validation are unchanged.
2. db.py: update_model and insert_model carry max_tokens (one more mutable column on update; defaulted on insert).
3. UI (dashboard/static/js/models.js + api.js + css): the models grid gains an integer input for max_tokens next to the flag controls (a number field, not a checkbox, since it is a value not a 0/1). The add-model form gains the same field, prefilled with the config default served from the API. api.js wrappers carry the field. Reuse existing input classes.
4. Tests: PUT edits max_tokens; POST insert respects the default and rejects <= 0 and over-bound with 422; the grid round-trips the value. Existing registry/write tests green.
5. Manual validation against a COPY (VACUUM INTO backup, DASHBOARD_DB_PATH copy, real Chrome): edit a model's max_tokens in the grid, run a generation, confirm the new cap is in effect (e.g. set a small cap on a reasoning model and observe the truncation/empty behavior the research predicts, then restore); add a model and confirm it gets the default; an over-bound value is rejected inline.
STOP at the gate with the full diff.

Out of scope (separable, note the adjacency)
- thinking_tokens capture (split reasoning tokens out on llm_invocations, per-provider field: openai reasoning_tokens, gemini thoughtsTokenCount, anthropic billed-in-output). It is a different table and a different concern (visibility, not capping) and can be its own bump. It pairs naturally with adding Opus but does not block this change.
- The Opus seed (price $5/$25 confirmed; model string pending one live API check) is just a new row through the existing path once max_tokens is per-model; a paid reasoning model is the main beneficiary of this change but is not required by it.
- The surrogate-key refactor rebuilds the models table; if it lands after this, it carries the max_tokens column forward. Coordinate the schema version number between the two plans so they do not collide.

Dependencies
- Phase 0 confirms the single-source constant and the call path before any edit.
- Phase 1 is behavior-preserving by construction (backfill == current value); Phase 2 only adds the editing surface on top. Each phase green at its STOP.