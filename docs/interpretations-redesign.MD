Feature: Interpretation on the Dashboard, structured JSON output, period-aware view-count chart. Write a multi-gate plan for review; do not code.
This relocates and restructures the existing Interpretation system (the generator with four provider adapters, llm_invocations, synthesize_lane/synthesize_run). Reuse that generator and the existing spend-panel factory; do not build new interpretation infrastructure. Phase it so the risky part (the JSON contract) is proven on a simple surface before the polished UI is built on top.
Phase 1: the structured JSON interpretation contract (the foundation, prove this first).

Change the interpretation generator to emit structured JSON, not prose. Schema is keyed by Dashboard chart section, plus two whole-window keys. Sections match the Dashboard's actual chart sections (growth, survivorship/rank, engagement, lifespan, whatever the tab renders); pin the exact keys against the current Dashboard layout. Top-level keys: the per-section interpretations, plus recommendation and a context-mode marker.
recommendation is a whole-window content suggestion for next week's videos, and it must be grounded: shape it as { "suggestion": "...", "based_on": ["<section keys it drew from>"] } so a suggestion cites the sections that justify it. No free-floating advice.
Context-mode switch: when an interpretation is run, the caller chooses aggregated (feed the model the computed lifecycle aggregates/medians/cohort outputs already produced) or raw (feed the underlying rows). Store which mode produced each interpretation in llm_invocations so aggregated-vs-raw can be compared later. This is a validation harness: same window, two modes, compare whether the narrative matches.
Token/spend design: aggregated mode is fixed-size and cheap, runs freely. Raw mode is the token exposure (a long window is the unbounded firehose), so raw specifically needs a spend cap and a pre-run cost/row-count estimate surfaced before running, reusing the existing spend-breaker pattern. Do not let the toggle hide that raw is potentially 30x aggregated.
Strict schema validation on the model's output: define the schema once, validate on receipt, and degrade gracefully (show what parsed, flag what didn't) rather than blank on a malformed or partial response. Same pin-the-contract-validate-both-sides discipline as the drill-down work.
Verify: valid schema-conformant JSON out for both context modes; malformed/partial response degrades gracefully; raw mode is capped and estimates cost before running; the context mode is recorded in llm_invocations.

Phase 2: relocate Interpretation to the Dashboard as a sticky panel, whole-JSON display, and move the view-count chart over.

Interpretation moves onto the Dashboard page as a sticky (position: sticky) right-rail panel, upper-right, that stays in view as the charts scroll in the main column. For this phase, display the entire parsed JSON (all sections plus the recommendation), no scroll-sync yet. This proves the schema and the panel with a trivial display before the scroll magic.
Move the view-count distribution bar chart from Trends to the Dashboard, and make it period-aware, keyed to the selected time filter (not "this run"). Locked specifics:

Population = the lane's deduped videos over the selected window (the standard rankings membership query), not one run.
Count each video once (distinct videos), bucketed by view count. Not per-appearance.
View count = the video's latest view count in the window (snapshot-backed), so buckets reflect current size and it's consistent with the stats_snapshots model.


Retitle to "View-count distribution" (drop "this run"); caption states it's the distinct lane videos over the selected period bucketed by their latest view count. A single-run window collapses to today's behavior naturally. This is a real backend change (another consumer of the population query), not a relocation, scope it as such.


Verify: sticky panel renders and holds position on scroll; full JSON displays; graceful degrade on a bad response; the view-count chart is window-aware, counts distinct videos once, uses latest-in-window view count, and the label matches the behavior.

Phase 3: scroll-sync the panel (pure frontend enhancement on the proven contract).

Upgrade the sticky panel so it shows the section-interpretation matching the chart currently in view as the user scrolls (growth section in view shows the growth key, etc.), with the recommendation shown at the appropriate point (top or end). The JSON keys map to the Dashboard sections. This is where "visible next to each graph" is realized.
Verify: scrolling to each chart section shows the matching interpretation; the recommendation is reachable; no jank; degrades to whole-panel if a section key is missing.

Phase 4 (separate cleanup, after 1-3 land): retire the rest of the Trends page.

Once Interpretation and the view-count chart have both moved to the Dashboard and nothing depends on the old page, remove Trends. Before deleting, grep that fetch_rank_history, renderBump, and renderTrajectory (kept untouched all session specifically for Trends) are not used elsewhere; remove whatever becomes dead. This is its own gate, not folded into the feature build.

Constraints carried: reuse the existing interpretation generator and spend-panel factory (no duplicate infrastructure); no schema change unless a phase explicitly needs one (declare it); no magic literals (bucket edges, caps, section keys as named constants); bounded SQL via the rankings membership subquery; Eastern instant-correct via _parse_instant; the raw-context spend cap reuses the existing breaker pattern; no em dashes; smoke against a VACUUM INTO copy bound to 127.0.0.1; re-grep anchors; stage explicit paths; no co-author trailer. Every phase ends in a STOP.
Write the plan with the JSON schema (the exact section keys against the current Dashboard layout, the recommendation shape, the context-mode field) specified first, since everything hangs off it. STOP for review before any code.