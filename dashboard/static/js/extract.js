// Extract page: the discovery runner. A controls panel (a Primary and a Fallback
// classifier model, a "Run discovery" button, a "Dry run" preview, a stopwatch, and a
// status pill) sits above a monospace terminal that streams the run's progress.
//
// Phase 2 builds the SHELL ONLY: the selects are empty (Phase 4 populates them from the
// model registry), the buttons have no handlers (Phase 3 opens the SSE stream and wires
// them), the terminal shows a resting hint until streamed, and the status pill is an empty
// slot. The right-column spend aside (#extract-spend-panel) is a sibling owned by spend.js
// in Phase 5; this module never touches it.
//
// Mirrors interpretation.js: it owns #extract-main only and builds the card + terminal as
// the two children of that mount, exactly as Interpretation stacks its controls above the
// result card. Empty temperature/seed/think controls are intentionally absent: the
// classifier runs deterministic.

let el = null;          // the page mount (#extract-main); this module owns ONLY this
let controls = null;    // handles kept for Phases 3-4 (no listeners attached yet)
let built = false;      // the scaffold is built once, on first entry

export function init(mount) {
  el = mount;
}

// Build a DOM node with text assigned via textContent ONLY (never innerHTML): the streamed
// run output is untrusted process stderr, so Phase 3 appends it as text + CSS pre-wrap.
function node(tag, opts = {}, children = []) {
  const n = document.createElement(tag);
  if (opts.class) n.className = opts.class;
  if (opts.text != null) n.textContent = opts.text;
  if (opts.type != null) n.type = opts.type;
  if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) n.setAttribute(k, v);
  for (const c of children) n.appendChild(c);
  return n;
}

// A caption stacked above its control (reuses the Interpretation control vocabulary).
function labeled(text, input) {
  return node("label", { class: "control-label interpret-control" }, [
    node("span", { text }),
    input,
  ]);
}

function buildScaffold() {
  // Two classifier model selects (empty until Phase 4). They reuse .interpret-select so
  // they match the Interpretation Model dropdown exactly.
  const primarySelect = node("select", {
    class: "interpret-select", attrs: { "aria-label": "Primary model" },
  });
  const fallbackSelect = node("select", {
    class: "interpret-select", attrs: { "aria-label": "Fallback model" },
  });

  // Primary action + free preview. Both carry .interpret-run so their padding (height)
  // matches, and the pair rides on the run-group's own row like Interpretation's Run.
  const runBtn = node("button", {
    class: "btn-primary interpret-run", type: "button", text: "Run discovery",
  });
  const dryRunBtn = node("button", {
    class: "btn-secondary interpret-run", type: "button", text: "Dry run (preview quota)",
  });

  // Run-time readout: a resting "—" that Phase 3 ticks live then freezes to the run's time.
  const stopwatchEl = node("span", {
    class: "interpret-stopwatch", text: "—", attrs: { "aria-live": "polite" },
  });
  const runtime = node("div", { class: "interpret-runtime" }, [
    node("span", { class: "control-label", text: "Run time" }),
    stopwatchEl,
  ]);

  // Status-pill slot: empty now; Phase 3 fills it from the terminal `result` event
  // (code -> {label, severity}).
  const statusPill = node("span", { class: "extract-status" });

  // Inline error/status line (reuses the Interpretation error styling).
  const errorEl = node("p", { class: "interpret-error", attrs: { role: "alert" } });

  const runGroup = node("div", { class: "interpret-run-group" }, [
    runBtn, dryRunBtn, runtime, statusPill,
  ]);

  const card = node("section", { class: "glass-panel interpret-controls" }, [
    node("h2", { class: "heading-sm", text: "Run extraction" }),
    node("div", { class: "interpret-controls-row" }, [
      labeled("Primary model", primarySelect),
      labeled("Fallback model", fallbackSelect),
      runGroup,
    ]),
    errorEl,
  ]);

  // The terminal: the streamed-output surface, stacked below the card inside #extract-main
  // (mirrors where Interpretation shows its result). aria-live is scoped HERE, not on the
  // page <main>, so only run output announces (Phase 3 revisits the politeness of a fast
  // log flood). Empty until a run; a muted resting hint stands in.
  const terminal = node("div", {
    class: "extract-terminal", attrs: { "aria-live": "polite" },
  }, [
    node("span", {
      class: "extract-terminal-rest",
      text: "Run a discovery to stream its progress here.",
    }),
  ]);

  controls = { primarySelect, fallbackSelect, runBtn, dryRunBtn, stopwatchEl, statusPill,
               errorEl, terminal };

  // Owns #extract-main ONLY (card + terminal). Never touches the sibling spend aside.
  el.replaceChildren(card, terminal);
}

function ensureBuilt() {
  if (built) return;
  buildScaffold();
  built = true;
}

// Page entry. Phase 2 only ensures the shell exists; Phase 3/4 add the stream + select
// population. The signature takes `state` for parity with the other page modules.
export function refresh(_state) {
  ensureBuilt();
}
