Plan: add Ollama as a provider and qwen3.5:9b as a model option

Objective
Make a local Ollama model selectable in the interpretation generator and the /models editor, end to end: provider registered, adapter built, capability flags verified, model seeded with a $0 price window so it shows in the dropdown and prices to $0 in spend. Target model is the locally installed Ollama tag (assumed qwen3.5:9b, to be confirmed against `ollama list` in Phase 0). Local inference is free, so the pricing path must produce $0 per invocation, not a missing-price gap.

Standing rules
Plan-before-execute, one phase at a time, explicit STOP gate, CC brings the diff. No em dashes. No magic numbers. Code wins over docs; read source before asserting. Verify the exact model string and capabilities against the live Ollama install (`ollama list`, `ollama show`), never from memory or this doc. WAL safety: any step touching live swipefile.db takes a verified VACUUM INTO backup first and smokes against a copy via DASHBOARD_DB_PATH, never the live seed. All writes through db.run_with_db_retry (outer) + db.transaction (inner). No auth (local single-user tool).

Open facts this plan assumes and Phase 0 must confirm or correct
- The exact installed tag is qwen3.5:9b. Confirm with `ollama list`; correct everywhere if different.
- split_model splits on the FIRST colon, so canonical `ollama:qwen3.5:9b` yields provider `ollama`, id `qwen3.5:9b`. If it splits on the last colon or expects exactly one colon, the double-colon tag breaks and the plan changes. This is the single most important check; do it first.
- `ollama` is not currently in llm.SUPPORTED_PROVIDERS (so POST /api/models 422s today).
- No Ollama adapter exists on the generate() seam (anthropic/openai/xai/google only).
- The interpretation dropdown requires a current price (valid_to IS NULL AND deleted = 0), so a $0 window is required for the model to appear.

Cross-cutting rules (carried from the registry work)
- provider is derived, never trusted from the client: store split_model(model)[0].
- A wrong capability flag fails at generation, surfaced inline by Fix A. So flags come from `ollama show`, not assumed.
- Pricing is per-invocation then summed; price is derived at display, never stored. A $0/$0 window is a valid non-negative window and must compute exactly $0 for every Ollama invocation, additive with everything else.
- No new half-open predicate variant. The $0 window obeys the same valid_from <= D AND (valid_to IS NULL OR D < valid_to) rule.

Phase 0: discovery, verify the tag and the colon, no code
Goal: replace every assumption above with a cited fact before writing anything.
Scope:
- CC reads split_model (cite file/line) and reports exactly how it handles a string with two colons. Show the parsed (provider, model_id) for the literal `ollama:qwen3.5:9b`. If it does not yield (`ollama`, `qwen3.5:9b`), STOP and report; the canonical format question must be resolved before proceeding.
- CC reports llm.SUPPORTED_PROVIDERS verbatim (type and members) and where it is defined. Confirm `ollama` is absent.
- CC reports the adapter dispatch: what it keys on (provider string vs enum), where the four current adapters live, and whether any shared code assumes a cloud SDK, an API key env var, or a remote base URL per provider. Identify what an Ollama adapter must satisfy.
- CC reports the Fix A error-wrap pattern (the per-adapter SDK-base-class catch) so the Ollama adapter mirrors it.
- Live verification (Mary runs, or CC if it has shell access to the Ollama host): `ollama list` for the exact installed tag string; `ollama show qwen3.5:9b` for context length, whether temperature and seed are honored, and the default params. These determine supports_temperature and supports_seed; do not set them from memory.
- Confirm the Ollama endpoint the adapter will call (default local http://127.0.0.1:11434) and decide where that base URL lives in config (no magic URL in adapter code).
Output: findings note with cites + the confirmed canonical string + the verified flags. No table or adapter work.

Phase 0 validation (must all pass before Phase 1 starts):
- split_model(`ollama:qwen3.5:9b`) returns exactly (`ollama`, `qwen3.5:9b`), shown in the findings with the source line. If not, STOP; the canonical-format question is unresolved.
- SUPPORTED_PROVIDERS reported verbatim with its type; `ollama` confirmed absent.
- Adapter dispatch keying identified (cite); confirmed whether shared adapter code assumes an API key or remote SDK that the Ollama adapter must bypass.
- `ollama list` output pasted, showing the exact installed tag string (confirms or corrects qwen3.5:9b).
- `ollama show qwen3.5:9b` output pasted, showing context length and whether temperature and seed are honored (sets the two flags).
- Ollama base URL confirmed reachable (a manual curl to /api/tags or one `ollama run` round-trip).
STOP.

Phase 1: provider + adapter + think capture + seed, end to end
Goal: ollama is a first-class provider with a working adapter; qwen3.5:9b is seeded with a $0 price window and appears in the dropdown and prices to $0 in spend; think is a per-run toggle gated on capability, with the applied value captured and displayed alongside temp and seed. Cross-provider plumbing (the think seam, the nullable thinking result field, interpretations.think) is designed once for all adapters, not as an Ollama-only branch.
Preconditions: Phase 0 approved. The colon split is resolved (partition first-colon, regression-tested). supports_seed is PROVISIONAL until step 1 proves it. If the seed proof fails, supports_seed flips to 0 and the seed claims drop, but the phase proceeds.

Steps:

0. Prove the provisional seed flag first (live, before anything relies on reproducibility). Run a high-entropy multi-sentence generation against qwen3.5:9b twice at a fixed seed and a nonzero temperature; then once with seed off. Seed-on pair byte-identical AND seed-off different confirms seed is honored -> supports_seed stays 1. If the seed-on pair diverges, set supports_seed = 0 and remove any reproducibility language from the seed row and tests. No DB writes; read-only against the local host. This gates the value seeded in step 7 and the assertions in step 10.

1. Schema bump to v8 (additive, migration discipline: add columns first, seed second, stamp version last). Add interpretations.think INTEGER NULL (the applied think value per run; NULL when the model is not reasoning-capable so think did not apply, same nullable-applied-value semantics as temperature and seed). Same non-destructive init_db path. Update testing/test_db.py inventory (the new column) and the version assertion to v8. No other column changes.

2. GenerateResult gains a nullable thinking field (cross-provider shape change, not Ollama-only). Today: text, input_tokens, output_tokens, seed_applied. Add thinking (content and/or thinking-token count) as Optional, populated only when an adapter produced thinking output; the four cloud adapters leave it None. This keeps the shared result uniform so the persistence and display layers read one shape regardless of provider.

3. Extend the generate() seam with think, capability-gated. Add think to the call contract the same way temperature and seed are gated: pass think to an adapter only when the model row is reasoning-capable (is_reasoning = 1); otherwise it is omitted and the applied value persists as NULL. generate() normalizes think (not is_reasoning-capable -> None) before any adapter sees it, mirroring the existing seed normalization, so an adapter never receives think for an incapable model.

4. Register the provider. Add ollama to llm.SUPPORTED_PROVIDERS (the one constant; the /models picker reads it via /api/models providers, so the picker gets it for free). No second list.

5. Config for the base URL. Add the Ollama base URL (loopback 127.0.0.1:11434 per the bind rule) to settings.toml (stdlib tomllib, version-controlled), read through config like other settings. No hardcoded URL in the adapter. Context length is not stored as a literal; if needed, read it from ollama show or config.

6. Build the Ollama adapter on the generate seam. Bypass _require_key (local service, no API key) and call the local HTTP endpoint from the configured base URL, not a vendor SDK. Strip the ollama: prefix and call with the bare tag (qwen3.5:9b). Pass temperature only if supports_temperature, seed only if supports_seed, think only if reasoning-capable (per the gated seam). When think is on, read the response thinking field alongside response and the eval_count (which already includes generated thinking tokens) and populate GenerateResult.thinking + token counts; when off, thinking is None. Wrap the HTTP client base error so any connection/not-running/not-pulled failure becomes LLMError(f"ollama: {message}") (Fix A parity), surfaced inline, never a 500 or a hang.

7. Seed the model + a $0 window. One models row: model = ollama:qwen3.5:9b, provider derived via split_model, enabled = 1, deleted = 0, supports_temperature = 1, supports_seed per step 0, is_reasoning = 1 (stored capability: the model can think; this drives the runtime toggle and display, it is not a runtime mode). One open model_prices row: input_per_1m = 0, output_per_1m = 0, valid_from = today, valid_to NULL. Insert-if-empty (ON CONFLICT DO NOTHING) so re-run is a no-op. Reproducible in code and test-covered; the same row is also addable through the /models form once the provider is registered.

8. Generator UI think toggle + capture-write. When the selected model has is_reasoning = 1, show a think toggle next to the existing temp/seed controls (same per-capability show/hide shape); hidden for non-capable models. On generate, the chosen think value flows through the gated seam; upsert_interpretation writes the applied think to interpretations.think (NULL when not capable), tested for overwrite-on-rerun like temp/seed.

9. Display think/temp/seed with each interpretation. The interpretation view shows the provenance for that run: think on/off, temperature, seed, with NULLs rendered as not-applicable. When think was on, the thinking content / larger token count is shown (or made available) per the captured GenerateResult.thinking; the larger token counts are intended, not a bug.

10. Tests (test-first where practical; each row is one assertion or a tight cluster).

   Seed proof (gates the flag):
   - The step 0 high-entropy comparison is captured as a recorded result; if seed-on diverged, supports_seed = 0 and no test asserts reproducibility.

   Provider + canonical-string contract:
   - split_model(ollama:qwen3.5:9b) == (ollama, qwen3.5:9b). The regression canary for the whole feature.
   - split_model(ollama:) yields an empty model id and POST /api/models rejects it 422 (empty-id guard).
   - ollama in SUPPORTED_PROVIDERS; GET /api/models providers includes it.

   Adapter capability gating (both directions):
   - supports_temperature 1 -> temperature passed; 0 -> omitted entirely (not None).
   - supports_seed 1 -> seed passed; 0 -> omitted.
   - is_reasoning 1 -> think passed when the run requests it; is_reasoning 0 -> think omitted and the persisted think is NULL.
   - The bare tag reaches Ollama: the ollama: prefix is stripped, call uses qwen3.5:9b.

   Think capture + result shape:
   - think on -> GenerateResult.thinking is populated and interpretations.think persists the applied value; think off -> thinking None and interpretations.think persists 0.
   - Non-reasoning-capable model -> think never sent, interpretations.think persists NULL.
   - The four cloud adapters leave GenerateResult.thinking None (no regression to the shared shape).
   - upsert_interpretation overwrite-on-rerun updates think alongside temp/seed.

   Adapter error-wrap (Fix A parity):
   - A simulated client/connection error becomes LLMError(f"ollama: {message}"); the raw exception does not escape as a 500.
   - Model-not-pulled / not-running surfaces the same clean ollama:-prefixed LLMError.

   Pricing ($0 is the point of failure to prove):
   - One Ollama invocation against the $0 window computes exactly 0.0 (not None, not error), thinking tokens included in the count but still priced at 0.
   - A month with one $0 Ollama invocation and one priced cloud invocation sums to exactly the cloud cost (additive, no corruption, no zeroing).
   - The $0 window obeys the shared half-open predicate at the boundaries (generated_at == valid_from included, == valid_to excluded).
   - Mid-month change to a nonzero price auto-closes the $0 window and prices each invocation against its own window additively (the $0 case is not special-cased).

   Dropdown + soft-delete semantics:
   - The seeded model appears in fetch_dropdown_models (it has a current price).
   - Soft-deleting its $0 window drops it from fetch_dropdown_models but fetch_spend still prices its past invocations at $0.
   - Soft-deleting the model row hides it from the dropdown but does not change spend.
   - Existing registry/write tests stay green.

   Add-form integration:
   - Provider ollama + typed qwen3.5:9b composes ollama:qwen3.5:9b and inserts (double-colon path through the form).
   - Pasted full ollama:qwen3.5:9b also inserts; the grid row provider chip reads ollama (derived from the stored string).

   Schema:
   - test_db.py inventory includes interpretations.think; version assertion == v8.

11. Manual validation (browser + real generation) against a COPY. WAL safety: VACUUM INTO backup first, point the dashboard at the copy via DASHBOARD_DB_PATH, drive real Chrome via playwright-core, never the live seed. Ollama running with qwen3.5:9b pulled.

   Setup:
   - python -m pytest testing/ dashboard/tests/ all green, including every new test and the unchanged prior suite.
   - ollama ps shows the model loadable; the configured base URL responds.

   Forward use (the generator):
   - /models provider picker lists ollama; pick it, type qwen3.5:9b, set flags, Add model -> row ollama:qwen3.5:9b with the chosen flags, no malformed error.
   - Add a $0 price window (0/0, valid_from today) -> appears in the interpretation dropdown.
   - The think toggle appears for this model (is_reasoning) and is hidden for a non-reasoning model.
   - Run one interpretation with think ON -> completes; the interpretation view shows think on, the temp/seed used, and the thinking content / larger token count; interpretations.think persists 1.
   - Run again with think OFF -> clean final answer only, smaller token count, interpretations.think persists 0.

   Pricing:
   - Both runs show $0.000000; month-to-date total otherwise unchanged.
   - A priced cloud run in the same month still totals correctly alongside the $0 Ollama runs.

   Failure path:
   - Stop Ollama (or rename the tag) and run -> clean inline ollama:-prefixed error, not a 500, not a hang.

   Soft-delete:
   - Soft-delete the $0 window -> gone from the dropdown, past runs still price at $0; restore -> back.

   Regression:
   - The four cloud providers still appear, price, and (where applicable) generate as before; their interpretations show think as not-applicable.
   STOP at the gate with the full diff.

Files: db.py (v8 schema add + inventory, seed), testing/test_db.py (inventory + version), llm.py (SUPPORTED_PROVIDERS, generate() think gating + seed normalization, GenerateResult thinking field, new Ollama adapter + dispatch, Fix A wrap), settings.toml + config.py (base URL), seed routine, dashboard generator JS (think toggle gated on capability) + interpretation view (think/temp/seed display), upsert_interpretation (write think), tests. The provider picker needs no change (already constant-fed).

Out of scope (deferred unless wanted)
- Multiple Ollama models or auto-discovery from ollama list. This adds one verified model; more are more rows through the same path.
- A local/free badge in the UI. Pricing already computes $0; a visual marker is a separate enhancement.
- Surfacing the full thinking transcript as a first-class, searchable artifact (beyond showing it with the interpretation). Capture is in; rich presentation is its own enhancement.

Dependencies
- Step 0 (seed proof) gates the seed flag and its tests; run it before relying on reproducibility.
- The v8 bump (step 1), the GenerateResult thinking field (step 2), and the gated think seam (step 3) are shared plumbing the Ollama adapter (step 6) and the UI (steps 8-9) build on; design and review them as one unit at the gate.
- 