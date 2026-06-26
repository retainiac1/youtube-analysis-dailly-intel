// Extract page: the discovery runner. A controls panel (a Primary and a Fallback
// classifier model, a "Run discovery" button, a "Dry run" preview, a stopwatch, and a
// status pill) sits above a monospace terminal that streams the run's progress.
//
// Phase 3 wires the run: clicking a button opens GET /api/extract/stream (via fetch, NOT
// EventSource: we need the 409/422 status and must not auto-reconnect/relaunch on the
// server's stream-close), relays each `log` event into the terminal, and ends on the
// terminal `result` event with a status pill from the pipeline exit contract. The selects
// stay empty until Phase 4 (the run reads its classifier models from prefs server-side, so
// it works without them); the #extract-spend-panel aside is Phase 5's (never touched here).
//
// Mirrors interpretation.js: it owns #extract-main only and builds the card + terminal as
// the two children of that mount. Temperature/seed/think are intentionally absent: the
// classifier runs deterministic.

import * as api from "./api.js";

let el = null;          // the page mount (#extract-main); this module owns ONLY this
let controls = null;    // the built handles (selects, buttons, stopwatch, pill, terminal)
let built = false;      // the scaffold is built once, on first entry
let running = false;    // a run is in flight (client single-flight; the server 409 is the
                        // real authority across tabs/reloads)
let timer = null;       // the live run-time stopwatch interval
let currentRun = null;  // the in-flight run's AbortController (FRESH per run; see onRun)
let modelOptions = [];  // the dropdown model ids (Phase 4 classifier picker)
let lastSaved = { primary: null, fallback: null };  // the last persisted pair (revert target)
let populatePromise = null;  // memoizes the one model-list fetch + select wiring

// Cap the retained terminal lines so a long run cannot grow the DOM without bound. A named
// JS const (like main.js's MAX_TRACKED) since there is no JS<->config bridge; the oldest
// line is dropped past the cap. Sized far above a normal discover's line count.
const MAX_TERMINAL_LINES = 2000;

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

// Milliseconds -> "3.2s" (mirrors interpretation.js); null when unmeasured.
function formatDuration(ms) {
  if (ms == null || Number.isNaN(ms)) return null;
  return `${(ms / 1000).toFixed(1)}s`;
}

// Map a pipeline result (exit code + reason slug) to the status pill's label + severity.
// Keyed by REASON, not code: `timeout` and `other` are both code 5 but read differently. An
// unknown reason (a future backend slug) falls back to a neutral pill showing the code, so
// the UI never hides a result it does not recognize. Exported for the browser unit test.
const STATUS_BY_REASON = {
  ok:                       { label: "Complete",            severity: "success" },
  no_rows:                  { label: "Ran, no new rows",    severity: "warn" },
  quota_exceeded:           { label: "Quota reached",       severity: "warn" },
  classify_budget_exceeded: { label: "Classify budget hit", severity: "warn" },
  network:                  { label: "Network error",       severity: "error" },
  auth:                     { label: "Auth error",          severity: "error" },
  db_locked:                { label: "Database busy",        severity: "error" },
  other:                    { label: "Failed",              severity: "error" },
  timeout:                  { label: "Timed out",           severity: "error" },
};

export function statusForResult(code, reason) {
  return STATUS_BY_REASON[reason]
    || { label: `Finished (code ${code})`, severity: "neutral" };
}

// Parse one SSE frame ("event: X\ndata: {json}") into { event, data }. Returns null for a
// keep-alive comment frame (a line starting with ':') or a frame with no event line. The
// data line is JSON (the server JSON-encodes payloads). Exported for the browser unit test.
export function parseSseFrame(frame) {
  let event = null;
  let dataRaw = null;
  for (const line of frame.split("\n")) {
    if (line.startsWith(":")) continue;             // comment / keep-alive
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataRaw = line.slice(5).trim();
  }
  if (!event) return null;
  let data = null;
  if (dataRaw != null) {
    try { data = JSON.parse(dataRaw); } catch (_) { data = null; }
  }
  return { event, data };
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

  // Status pill: the page's polite live region (role=status + aria-live=polite), so only
  // the run's status transitions announce (Running -> Complete/Failed), not the log flood.
  // Filled from the terminal `result` event via statusForResult().
  const statusPill = node("span", {
    class: "extract-status", attrs: { role: "status", "aria-live": "polite" },
  });

  // Inline error/status line (reuses the Interpretation error styling).
  const errorEl = node("p", { class: "interpret-error", attrs: { role: "alert" } });
  // A neutral inline note (not an error): used when a select is auto-advanced to keep the
  // two providers distinct, so the adjustment is visible rather than silent.
  const noteEl = node("p", { class: "extract-note" });
  // Subtitle reflecting the once-a-day cap: set from /api/run-state on load (and after
  // each run). When a discover already ran today, the button reads "Refresh stats" and
  // this explains why. Its own node so the pair-change note never clobbers it.
  const runStateNote = node("p", { class: "extract-note" });

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
    noteEl,
    runStateNote,
  ]);

  // The terminal: the streamed-output surface, stacked below the card inside #extract-main
  // (mirrors where Interpretation shows its result). aria-live is OFF: a fast discover emits
  // many lines, which would flood a screen reader; the status pill carries the polite live
  // region instead, announcing only the run's status transitions. Empty until a run; a
  // muted resting hint stands in.
  const terminal = node("div", {
    class: "extract-terminal", attrs: { "aria-live": "off" },
  }, [
    node("span", {
      class: "extract-terminal-rest",
      text: "Run a discovery to stream its progress here.",
    }),
  ]);

  controls = { primarySelect, fallbackSelect, runBtn, dryRunBtn, stopwatchEl, statusPill,
               errorEl, noteEl, runStateNote, terminal };

  // Owns #extract-main ONLY (card + terminal). Never touches the sibling spend aside.
  el.replaceChildren(card, terminal);

  runBtn.addEventListener("click", () => onRun("discover"));
  dryRunBtn.addEventListener("click", () => onRun("dry-run"));
}

function ensureBuilt() {
  if (built) return;
  buildScaffold();
  built = true;
}

// --- classifier model picker (Phase 4) --------------------------------------

function providerOf(v) {
  return v ? v.split(":")[0] : "";
}

// Fill one select with an <option> per dropdown model, pre-selecting `current`. If
// `current` is no longer a dropdown option (it lost its price window) it is still the
// effective pref, so prepend it as a marked, display-only "(unavailable)" option rather
// than silently showing a different model.
function fillSelect(select, current) {
  const opts = [];
  if (current && !modelOptions.includes(current)) {
    opts.push(node("option", {
      text: `${current} (unavailable)`,
      attrs: { value: current, "data-stale": "true" },
    }));
  }
  for (const m of modelOptions) {
    opts.push(node("option", { text: m, attrs: { value: m } }));
  }
  select.replaceChildren(...opts);
  if (current) select.value = current;
}

// Disable (for future selection) every option in `select` whose provider collides with
// `otherValue`'s provider — never the currently-selected option (so the value is preserved)
// and never a stale display option.
function disableColliding(select, otherValue) {
  const otherProv = providerOf(otherValue);
  for (const opt of select.options) {
    opt.disabled = opt.value !== select.value
      && opt.dataset.stale !== "true"
      && providerOf(opt.value) === otherProv;
  }
}

function syncPairConstraints() {
  disableColliding(controls.fallbackSelect, controls.primarySelect.value);
  disableColliding(controls.primarySelect, controls.fallbackSelect.value);
}

// The first enabled, REAL (non-stale, in-dropdown) option in `select` whose provider
// differs from `againstProvider`; null when none exists.
function firstValidOption(select, againstProvider) {
  for (const opt of select.options) {
    if (opt.dataset.stale === "true") continue;
    if (!modelOptions.includes(opt.value)) continue;
    if (providerOf(opt.value) === againstProvider) continue;
    return opt.value;
  }
  return null;
}

async function onPairChange(changedSelect) {
  const isPrimary = changedSelect === controls.primarySelect;
  const other = isPrimary ? controls.fallbackSelect : controls.primarySelect;
  const otherSlot = isPrimary ? "fallback" : "primary";
  const changedProv = providerOf(changedSelect.value);
  controls.errorEl.textContent = "";
  controls.noteEl.textContent = "";

  syncPairConstraints();

  // Disabling an option does not move the current selection, so a change can leave the
  // other select on a now-colliding value. Auto-advance it to a valid option before saving.
  let note = "";
  if (providerOf(other.value) === changedProv) {
    const saved = lastSaved[otherSlot];
    const next = (saved && modelOptions.includes(saved) && providerOf(saved) !== changedProv)
      ? saved
      : firstValidOption(other, changedProv);
    if (next == null) {
      controls.errorEl.textContent =
        "No different-provider model is available for the other slot.";
      return;
    }
    other.value = next;
    syncPairConstraints();
    note = `${otherSlot === "fallback" ? "Fallback" : "Primary"} set to ${next} `
         + "to keep the two providers distinct.";
  }

  await persistPair(note);
}

// Persist the current pair. Disables BOTH selects while the POST is in flight, so two
// quick edits cannot land their responses out of order. On a 422 (a genuine backstop now
// that the UI prevents collisions) revert to the last saved pair and show the detail.
async function persistPair(note) {
  const primary = controls.primarySelect.value;
  const fallback = controls.fallbackSelect.value;
  controls.primarySelect.disabled = true;
  controls.fallbackSelect.disabled = true;
  try {
    await api.setClassificationModels(primary, fallback);
    lastSaved = { primary, fallback };
    controls.noteEl.textContent = note;
    controls.errorEl.textContent = "";
  } catch (err) {
    controls.primarySelect.value = lastSaved.primary;
    controls.fallbackSelect.value = lastSaved.fallback;
    syncPairConstraints();
    controls.noteEl.textContent = "";
    controls.errorEl.textContent = err.message || String(err);
  } finally {
    controls.primarySelect.disabled = false;
    controls.fallbackSelect.disabled = false;
  }
}

async function populateModels() {
  let data;
  try {
    data = await api.getClassificationModels();
  } catch (err) {
    controls.errorEl.textContent = `Could not load model options: ${err.message || err}`;
    return;
  }
  modelOptions = data.options || [];
  lastSaved = { primary: data.primary, fallback: data.fallback };
  fillSelect(controls.primarySelect, data.primary);
  fillSelect(controls.fallbackSelect, data.fallback);
  syncPairConstraints();
  controls.primarySelect.addEventListener(
    "change", () => onPairChange(controls.primarySelect));
  controls.fallbackSelect.addEventListener(
    "change", () => onPairChange(controls.fallbackSelect));
}

function ensurePopulated() {
  if (!populatePromise) populatePromise = populateModels();
  return populatePromise;
}

// --- run / stream -----------------------------------------------------------

function setPill(label, severity) {
  controls.statusPill.textContent = label;
  controls.statusPill.className = `extract-status extract-status-${severity}`;
}

// Append one streamed log line. textContent only (the line is untrusted process stderr).
// Caps the retained lines, and auto-scrolls only when the view is already pinned to the
// bottom, so a user who scrolled up to read is not yanked back down.
function appendLine(text) {
  const t = controls.terminal;
  const pinned = t.scrollHeight - t.scrollTop - t.clientHeight < 4;
  t.appendChild(node("div", { class: "extract-terminal-line", text }));
  while (t.childElementCount > MAX_TERMINAL_LINES) t.removeChild(t.firstChild);
  if (pinned) t.scrollTop = t.scrollHeight;
}

async function onRun(mode) {
  if (running) return;                      // client single-flight (per tab)
  running = true;
  controls.runBtn.disabled = true;
  controls.dryRunBtn.disabled = true;
  controls.errorEl.textContent = "";
  setPill("Running…", "running");
  controls.terminal.replaceChildren();      // drop the resting hint / a prior run's lines

  const start = performance.now();
  const tick = () => {
    controls.stopwatchEl.textContent = formatDuration(performance.now() - start) || "—";
  };
  tick();
  timer = setInterval(tick, 100);

  // FRESH AbortController per run: a controller is one-shot, so reusing one across runs
  // would, once aborted, dead-letter every later fetch with AbortError. No caller aborts it
  // this phase (no cancel UI), but the fresh-per-run pattern is set now to prevent that bug.
  const ctrl = new AbortController();
  currentRun = ctrl;

  try {
    const res = await api.streamExtract(mode, ctrl.signal);
    if (res.status === 409) {
      // The server is the single-flight authority: a discover is already running (another
      // tab, or one that survived a reload). Show it; do NOT open a second stream. Returning
      // here still runs the finally, so the buttons re-enable.
      setPill("Busy", "warn");
      controls.errorEl.textContent = (await api.errorFrom(res)).message;
      return;
    }
    if (!res.ok) {
      setPill("Failed", "error");
      controls.errorEl.textContent = (await api.errorFrom(res)).message;
      return;
    }
    await consumeStream(res.body, start);
  } catch (err) {
    if (err && err.name === "AbortError") return;   // cancelled (no caller yet): silent
    setPill("Failed", "error");
    controls.errorEl.textContent = err.message || String(err);
  } finally {
    clearInterval(timer);
    timer = null;
    running = false;
    currentRun = null;
    controls.runBtn.disabled = false;
    controls.dryRunBtn.disabled = false;
    applyRunState();   // a just-completed discover flips the label to "Refresh stats"
  }
}

// Read the SSE body to completion. ONE persistent TextDecoder with {stream:true} so a
// multi-byte UTF-8 char split across read() chunks is decoded correctly (not a replacement
// char). Parse only COMPLETE frames (split on the blank line) and keep the trailing partial
// in `buf` for the next read; a TCP chunk can split a frame mid-boundary.
async function consumeStream(body, start) {
  const reader = body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let sawResult = false;

  const drain = () => {
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, i);
      buf = buf.slice(i + 2);
      const parsed = parseSseFrame(frame);
      if (!parsed) continue;                          // keep-alive comment / blank
      if (parsed.event === "log" && parsed.data) {
        appendLine(parsed.data.text);
      } else if (parsed.event === "result" && parsed.data) {
        sawResult = true;
        clearInterval(timer);                         // freeze the clock at the result
        timer = null;
        controls.stopwatchEl.textContent =
          formatDuration(performance.now() - start) || "—";
        const { label, severity } = statusForResult(parsed.data.code, parsed.data.reason);
        setPill(label, severity);
        // A real run carries its run_summary: show which path actually ran (mode) and the
        // catalog-change counts in the pane. Absent for a dry run (no summary written).
        const summary = parsed.data.summary;
        if (summary) {
          const cost = Number(summary.classify_cost || 0).toFixed(4);
          appendLine(
            `Run summary (${summary.mode}): added ${summary.added}, ` +
            `updated ${summary.updated}, aged_out ${summary.aged_out}, ` +
            `gone ${summary.gone}, snapshots ${summary.snapshot_count}, ` +
            `classify cost $${cost}`);
        }
        // EVERY terminal result (success or failure) may have spent classifier tokens, so
        // notify Phase 5's spend panel. Harmless now: no listener exists until Phase 5.
        document.dispatchEvent(new CustomEvent("extract:completed", { detail: { summary } }));
      }
    }
  };

  for (;;) {
    const { value, done } = await reader.read();
    if (value) buf += dec.decode(value, { stream: true });
    drain();                                          // parse whatever is now complete
    if (done) break;
  }
  // drain() ran after the final chunk, so a result frame that arrived with the close was
  // already parsed. Only if NO result EVER arrived is this a defective stream.
  if (!sawResult) {
    setPill("No result", "error");
    controls.errorEl.textContent = "The run ended without a result line.";
  }
}

// Page entry: ensure the shell exists, then populate the model selects once (the run
// reads its classifier models from prefs server-side, so the selects only persist the
// operator's choice). The signature takes `state` for parity with the other page modules.
export function refresh(_state) {
  ensureBuilt();
  ensurePopulated();
  applyRunState();   // re-read each load, so the label flips at the next Pacific day
}

// Label the primary button from the once-a-day-cap state. The button ALWAYS triggers a
// `discover` request; the resolver caps it to a refresh server-side, so this is purely
// cosmetic. Failure is non-blocking: default to the discover label.
async function applyRunState() {
  let ran = false;
  try {
    ran = await api.getRunState();
  } catch {
    ran = false;
  }
  controls.runBtn.textContent = ran ? "Refresh stats" : "Run discovery";
  controls.runStateNote.textContent = ran
    ? "Discovery already ran today (Pacific) — this runs a stats refresh only."
    : "";
}
