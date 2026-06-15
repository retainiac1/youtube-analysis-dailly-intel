// Prices page: the price-refresh agent's home. A controls row (the extraction-model
// dropdown + a Run button + a stopwatch) sits above two panels — the prices/deltas
// table (current price + day-over-day change per priced model) and the review list
// (pending >=10% proposals with Confirm/Reject). Run triggers POST /api/price-refresh/run
// and repaints the panels; confirm/reject resolve a proposal and repaint. Errors surface
// inline (role="alert"), never a raw 500 / alert. Mirrors interpretation.js (run +
// stopwatch) and models.js (render + inline errors).

import * as api from "./api.js";

let el = null;             // the page mount (<main id="prices">)
let pricesPanel = null;    // the prices/deltas panel body (repainted on reload)
let reviewPanel = null;    // the review panel body (repainted on reload)
let sourcesPanel = null;   // the "sources this run" provenance body (repainted on reload)
let stopwatchEl = null;    // frozen across panel reloads (only Run touches it)
let summaryEl = null;      // the run-summary line by the stopwatch (cleared on the next Run)
let activeModel = null;    // the persisted selection (to revert the select on a failed save)

export function init(mount) {
  el = mount;
}

// Build a DOM node with text via textContent ONLY (never innerHTML — the quote is
// extracted page text). Same idiom as the other modules.
function node(tag, opts = {}, children = []) {
  const n = document.createElement(tag);
  if (opts.class) n.className = opts.class;
  if (opts.text != null) n.textContent = opts.text;
  if (opts.type != null) n.type = opts.type;
  if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) n.setAttribute(k, v);
  for (const c of children) n.appendChild(c);
  return n;
}

function formatDuration(ms) {
  if (ms == null || Number.isNaN(ms)) return null;
  return `${(ms / 1000).toFixed(1)}s`;
}

function money(v) {
  return `$${Number(v).toFixed(2)}`;
}

function pctLabel(p) {
  return `${p > 0 ? "+" : ""}${(p * 100).toFixed(1)}%`;
}

// --- full render (controls + empty panels) ----------------------------------

export async function refresh() {
  if (!el) return;
  let data, modelData;
  try {
    [data, modelData] = await Promise.all([
      api.getPriceRefresh(),
      api.getExtractionModel(),
    ]);
  } catch (err) {
    el.replaceChildren(node("div", { class: "prices-page" }, [
      node("p", { class: "prices-empty prices-error",
                  text: `Could not load prices: ${err.message || err}` }),
    ]));
    return;
  }

  activeModel = modelData.model;
  pricesPanel = node("div", { class: "prices-table" });
  reviewPanel = node("div", { class: "prices-review" });
  sourcesPanel = node("div", { class: "prices-sources" });

  el.replaceChildren(node("div", { class: "prices-page" }, [
    node("header", { class: "prices-head" }, [
      node("h2", { class: "heading-sm", text: "Price refresh" }),
    ]),
    node("div", { class: "prices-top-row" }, [
      controlsRow(modelData),
      node("section", { class: "glass-panel prices-panel prices-sources-panel" }, [
        node("h3", { class: "heading-sm", text: "Sources this run" }),
        sourcesPanel,
      ]),
    ]),
    node("section", { class: "glass-panel prices-panel" }, [
      node("h3", { class: "heading-sm", text: "Prices (vs prior window)" }),
      node("p", { class: "prices-caption",
                  text: "$ / 1M tokens · each cell cross-checked against a validator feed" }),
      pricesPanel,
    ]),
    node("section", { class: "glass-panel prices-panel" }, [
      node("h3", { class: "heading-sm", text: "Pending review" }),
      reviewPanel,
    ]),
  ]));

  renderPanels(data);
}

// Decorative only: an abstract cyan->blue "price pulse" sparkline with a glowing
// leading node and concentric rings (the refresh sweep). Static markup, so it is
// injected via innerHTML; node() can't create SVG. aria-hidden -- purely visual.
const PRICES_GLYPH_SVG = `
<svg viewBox="0 0 280 132" role="presentation" focusable="false">
  <defs>
    <linearGradient id="prices-glyph-grad" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="var(--accent-cyan)"/>
      <stop offset="1" stop-color="var(--accent-blue)"/>
    </linearGradient>
  </defs>
  <g class="prices-glyph-rings">
    <circle cx="214" cy="58" r="16"/><circle cx="214" cy="58" r="30"/>
    <circle cx="214" cy="58" r="46"/>
  </g>
  <polyline class="prices-glyph-line"
    points="14,96 56,78 98,86 140,50 182,64 214,58"
    fill="none" stroke="url(#prices-glyph-grad)" stroke-width="2.5"
    stroke-linecap="round" stroke-linejoin="round"/>
  <circle class="prices-glyph-node" cx="214" cy="58" r="5"/>
</svg>`;

function controlsRow(modelData) {
  const modelSelect = node("select", {
    class: "interpret-select", attrs: { "aria-label": "Extraction model" } });
  for (const m of modelData.options || []) {
    modelSelect.appendChild(node("option", { text: m, attrs: { value: m } }));
  }
  if (modelData.model) modelSelect.value = modelData.model;
  const modelError = node("span", { class: "prices-error", attrs: { role: "alert" } });

  // Persist on change; revert the select and show the message if the server rejects it.
  modelSelect.addEventListener("change", async () => {
    modelError.textContent = "";
    const chosen = modelSelect.value;
    modelSelect.disabled = true;
    try {
      await api.setExtractionModel(chosen);
      activeModel = chosen;
    } catch (err) {
      modelError.textContent = err.message || String(err);
      modelSelect.value = activeModel;
    } finally {
      modelSelect.disabled = false;
    }
  });

  const runBtn = node("button", {
    class: "btn-primary prices-run", type: "button", text: "Run" });
  stopwatchEl = node("span", {
    class: "interpret-stopwatch", text: "—", attrs: { "aria-live": "polite" } });
  summaryEl = node("span", {
    class: "prices-run-summary", attrs: { "aria-live": "polite" } });
  const runError = node("span", { class: "prices-error", attrs: { role: "alert" } });
  runBtn.addEventListener("click", () => onRun(runBtn, runError));

  const glyph = node("div", { class: "prices-glyph", attrs: { "aria-hidden": "true" } });
  glyph.innerHTML = PRICES_GLYPH_SVG;

  return node("section", { class: "glass-panel prices-controls" }, [
    glyph,
    node("label", { class: "prices-model-field" }, [
      node("span", { class: "control-label", text: "Extraction model" }),
      modelSelect,
    ]),
    node("div", { class: "prices-run-group" }, [
      runBtn,
      node("div", { class: "interpret-runtime" }, [
        node("span", { class: "control-label", text: "Run time" }),
        stopwatchEl,
      ]),
      summaryEl,
    ]),
    modelError,
    runError,
  ]);
}

// The whole run (every provider x 2 sources) — longer/more variable than the interpret
// stopwatch. Freeze to the SERVER's measured duration, then repaint the panels only (the
// controls + frozen stopwatch stay put).
async function onRun(runBtn, runError) {
  runError.textContent = "";
  summaryEl.replaceChildren();          // same lifecycle as the timer reset
  runBtn.disabled = true;
  const start = performance.now();
  const tick = () => { stopwatchEl.textContent = formatDuration(performance.now() - start); };
  tick();
  const timer = setInterval(tick, 100);
  try {
    const res = await api.runPriceRefresh();
    clearInterval(timer);
    stopwatchEl.textContent = formatDuration(res.duration_ms) || "—";
    renderRunSummary(res);              // set BEFORE the panels-only repaint, never refresh()
    await reloadPanels();
  } catch (err) {
    clearInterval(timer);
    tick();
    runError.textContent = err.message || String(err);
  } finally {
    runBtn.disabled = false;
  }
}

// Headline count from `res.errors` (a LIST of (model, field, reason) field-level
// outcomes), but the count alone is opaque — so we also lift the REAL reasons that
// already ride along in `res`: failed source_outcomes (the root cause: an Ollama
// timeout, a 403, unparseable JSON) plus genuine rejects. A degraded run (errors
// present, still 200) shows the count in red and never says "no price changes", so it
// can't masquerade as a clean no-change run. The full per-source list still lives in
// the adjacent Sources panel; this just surfaces the why at a glance.
const REASON_DETAIL_CAP = 4;

function runIssueReasons(res) {
  const out = [];
  const seen = new Set();
  const push = (text) => {
    if (text && !seen.has(text)) { seen.add(text); out.push(text); }
  };
  // 1. Root causes: every failed scrape / validator, with its real reason.
  for (const o of (res.cross_check && res.cross_check.source_outcomes) || []) {
    if (o.ok) continue;
    const who = o.role === "scrape" ? `scrape · ${o.provider}` : `validator · ${o.name}`;
    push(`${who}: ${o.reason || "unknown"}`);
  }
  // 2. Genuine rejects — "no source data" is downstream of a failed scrape already
  //    shown in (1), so skip it to avoid restating the same failure.
  for (const [model, field, reason] of res.errors || []) {
    if (reason === "no source data") continue;
    push(`${model}${field ? ` ${field}` : ""}: ${reason}`);
  }
  return out;
}

function renderRunSummary(res) {
  const n = (res.errors || []).length;
  const changed = res.applied + res.staged + res.rejected;
  let base = `Applied ${res.applied} · staged ${res.staged} · `
    + `rejected ${res.rejected} · skipped ${res.skipped}`;
  if (changed === 0 && n === 0) base += " — no price changes";
  const children = [node("span", { class: "prices-run-summary-base", text: base })];
  if (n > 0) {
    children.push(node("span", { class: "prices-run-summary-error",
      text: ` · ${n} issue${n === 1 ? "" : "s"}` }));
    const reasons = runIssueReasons(res);
    for (const r of reasons.slice(0, REASON_DETAIL_CAP)) {
      children.push(node("span", { class: "prices-run-summary-detail", text: r }));
    }
    const extra = reasons.length - REASON_DETAIL_CAP;
    if (extra > 0) {
      children.push(node("span", { class: "prices-run-summary-detail",
        text: `+${extra} more` }));
    }
  }
  summaryEl.replaceChildren(...children);
}

// --- panel render (repainted after run / confirm / reject) ------------------

async function reloadPanels() {
  try {
    renderPanels(await api.getPriceRefresh());
  } catch (err) {
    pricesPanel.replaceChildren(
      node("p", { class: "prices-empty prices-error",
                  text: `Could not reload: ${err.message || err}` }));
  }
}

function renderPanels(data) {
  const crossCheck = data.cross_check || null;
  pricesPanel.replaceChildren(...renderPrices(data.prices || [], crossCheck));
  reviewPanel.replaceChildren(...renderReview(data.proposals || []));
  sourcesPanel.replaceChildren(...renderSources(crossCheck));
}

// Human-readable cell-flag labels. The five states are deliberately distinct: mismatch is
// the only alarm; unverified (the validator does not carry the model) is no-signal and
// muted; no-key-mapping (our config gap) is its own loud state, never folded into either.
const FLAG_LABELS = {
  match: "validator match",
  drift: "validator drift",
  mismatch: "validator mismatch",
  unverified: "no validator entry",
  no_key_mapping: "no key mapping",
};

// The cross-check chip for one field cell, from the run's provenance (cells[model][field]).
// null when there is no snapshot yet (no run) so the cell shows price-only.
function flagChip(cell) {
  if (!cell || !cell.flag) return null;
  const chip = node("span", {
    class: `prices-flag prices-flag-${cell.flag.replace(/_/g, "-")}`,
    text: FLAG_LABELS[cell.flag] || cell.flag });
  if (cell.validator != null) {
    chip.setAttribute("title", `validator: ${money(cell.validator)}`);
  }
  return chip;
}

// provider -> its scrape outcome (for the per-row pricing-page link), from the provenance.
function scrapeByProvider(crossCheck) {
  const map = {};
  if (crossCheck && crossCheck.source_outcomes) {
    for (const o of crossCheck.source_outcomes) {
      if (o.role === "scrape") map[o.provider] = o;
    }
  }
  return map;
}

// The Δ badge, only for the prior-present case (fieldCell guards prior == null).
function deltaBadge(f) {
  if (f.direction === "none") {
    return node("span", { class: "prices-delta prices-delta-none", text: "no change" });
  }
  return node("span", {
    class: `prices-delta prices-delta-${f.direction}`, text: pctLabel(f.pct) });
}

// label · current "$1.00" · prior "was $0.90" (or "no prior data") · Δ · cross-check chip —
// the dollar values are never confused, and the chip shows the validator's verdict per field.
function fieldCell(label, f, cell) {
  const cells = [
    node("span", { class: "control-label", text: label }),
    node("span", { class: "prices-value", text: money(f.current) }),
  ];
  if (f.prior == null) {
    // Single-window model: the empty-history state, NOT an error.
    cells.push(node("span", { class: "prices-prior prices-prior-empty",
                              text: "no prior data" }));
  } else {
    cells.push(node("span", { class: "prices-prior", text: `was ${money(f.prior)}` }));
    cells.push(deltaBadge(f));
  }
  const chip = flagChip(cell);
  if (chip) cells.push(chip);
  return node("div", { class: "prices-field" }, cells);
}

function renderPrices(prices, crossCheck) {
  if (!prices.length) {
    return [node("p", { class: "prices-empty", text: "No priced models." })];
  }
  const cellsByModel = (crossCheck && crossCheck.cells) || {};
  const scrapes = scrapeByProvider(crossCheck);
  return prices.map((p) => {
    const provider = p.model.split(":", 1)[0];
    const scrape = scrapes[provider];
    const modelCell = node("span", { class: "prices-model", text: p.model });
    const head = scrape
      ? node("div", { class: "prices-model-head" }, [
          modelCell,
          node("a", { class: "prices-source", text: "pricing page",
            attrs: { href: scrape.display_url || scrape.url, target: "_blank",
                     rel: "noopener" } }),
        ])
      : modelCell;
    if (p.input == null) {
      // A priced model that lost its open window — shown as a visible problem, not dropped.
      return node("div", { class: "prices-row prices-row-warn" }, [
        head,
        node("span", { class: "prices-warn", text: "no current price" }),
      ]);
    }
    const cells = cellsByModel[p.model] || {};
    return node("div", { class: "prices-row" }, [
      head,
      fieldCell("input", p.input, cells.input),
      fieldCell("output", p.output, cells.output),
    ]);
  });
}

// Every URL actually attempted this run, with its outcome — driven only by the provenance
// snapshot (the page never refetches the validators). A failed primary validator is shown
// alongside the answering fallback, never hidden.
function renderSources(crossCheck) {
  if (!crossCheck) {
    return [node("p", { class: "prices-empty", text: "No run yet." })];
  }
  const header = node("p", { class: "prices-caption",
    text: `Validator: ${crossCheck.validator_name || "none answered"}`
      + (crossCheck.run_at ? ` · ${crossCheck.run_at}` : "") });
  const rows = (crossCheck.source_outcomes || []).map((o) => {
    const role = o.role === "scrape"
      ? `scrape · ${o.provider}`
      : `validator · ${o.name}`;
    const status = o.ok
      ? `ok${o.status ? ` (${o.status})` : ""}`
      : `failed: ${o.reason || "unknown"}`;
    return node("div", {
      class: `prices-source-row${o.ok ? "" : " prices-source-row-failed"}` }, [
      node("span", { class: "prices-source-role", text: role }),
      node("a", { class: "prices-source", text: o.display_url || o.url,
        attrs: { href: o.display_url || o.url, target: "_blank", rel: "noopener" } }),
      node("span", { class: o.ok ? "prices-source-ok" : "prices-source-bad",
                     text: status }),
    ]);
  });
  return [header, ...rows];
}

function renderReview(proposals) {
  if (!proposals.length) {
    return [node("p", { class: "prices-empty", text: "No proposals awaiting review." })];
  }
  return proposals.map((p) => {
    const err = node("span", { class: "prices-error", attrs: { role: "alert" } });
    const confirmBtn = node("button", {
      class: "btn-primary", type: "button", text: "Confirm" });
    const rejectBtn = node("button", {
      class: "btn-secondary", type: "button", text: "Reject" });
    confirmBtn.addEventListener(
      "click", () => resolve(p.id, api.confirmProposal, confirmBtn, rejectBtn, err));
    rejectBtn.addEventListener(
      "click", () => resolve(p.id, api.rejectProposal, confirmBtn, rejectBtn, err));

    return node("div", { class: "prices-proposal" }, [
      node("div", { class: "prices-proposal-head" }, [
        node("span", { class: "prices-model", text: `${p.model} · ${p.field}` }),
        node("span", { class: `prices-delta prices-delta-${p.direction}`,
                       text: pctLabel(p.pct_change) }),
      ]),
      node("div", { class: "prices-proposal-move",
                    text: `${money(p.old_value)} → ${money(p.new_value)}` }),
      node("p", { class: "prices-quote", text: p.quote || "" }),
      node("a", { class: "prices-source", text: "source",
                  attrs: { href: p.source_url, target: "_blank", rel: "noopener" } }),
      node("div", { class: "prices-proposal-actions" }, [confirmBtn, rejectBtn]),
      err,
    ]);
  });
}

async function resolve(id, fn, confirmBtn, rejectBtn, err) {
  err.textContent = "";
  confirmBtn.disabled = true;
  rejectBtn.disabled = true;
  try {
    await fn(id);
    await reloadPanels();
  } catch (e) {
    err.textContent = e.message || String(e);
    confirmBtn.disabled = false;
    rejectBtn.disabled = false;
  }
}
