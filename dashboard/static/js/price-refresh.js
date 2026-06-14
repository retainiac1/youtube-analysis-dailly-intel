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
let stopwatchEl = null;    // frozen across panel reloads (only Run touches it)
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

  el.replaceChildren(node("div", { class: "prices-page" }, [
    node("header", { class: "prices-head" }, [
      node("h2", { class: "heading-sm", text: "Price refresh" }),
    ]),
    controlsRow(modelData),
    node("section", { class: "glass-panel prices-panel" }, [
      node("h3", { class: "heading-sm", text: "Prices (vs prior window)" }),
      pricesPanel,
    ]),
    node("section", { class: "glass-panel prices-panel" }, [
      node("h3", { class: "heading-sm", text: "Pending review" }),
      reviewPanel,
    ]),
  ]));

  renderPanels(data);
}

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
  const runError = node("span", { class: "prices-error", attrs: { role: "alert" } });
  runBtn.addEventListener("click", () => onRun(runBtn, runError));

  return node("section", { class: "glass-panel prices-controls" }, [
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
  runBtn.disabled = true;
  const start = performance.now();
  const tick = () => { stopwatchEl.textContent = formatDuration(performance.now() - start); };
  tick();
  const timer = setInterval(tick, 100);
  try {
    const res = await api.runPriceRefresh();
    clearInterval(timer);
    stopwatchEl.textContent = formatDuration(res.duration_ms) || "—";
    await reloadPanels();
  } catch (err) {
    clearInterval(timer);
    tick();
    runError.textContent = err.message || String(err);
  } finally {
    runBtn.disabled = false;
  }
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
  pricesPanel.replaceChildren(...renderPrices(data.prices || []));
  reviewPanel.replaceChildren(...renderReview(data.proposals || []));
}

function deltaBadge(f) {
  if (f.prior == null) {
    return node("span", { class: "prices-delta prices-delta-none", text: "no prior data" });
  }
  if (f.direction === "none") {
    return node("span", { class: "prices-delta prices-delta-none", text: "no change" });
  }
  return node("span", {
    class: `prices-delta prices-delta-${f.direction}`, text: pctLabel(f.pct) });
}

function fieldCell(label, f) {
  return node("div", { class: "prices-field" }, [
    node("span", { class: "control-label", text: label }),
    node("span", { class: "prices-value", text: money(f.current) }),
    deltaBadge(f),
  ]);
}

function renderPrices(prices) {
  if (!prices.length) {
    return [node("p", { class: "prices-empty", text: "No priced models." })];
  }
  return prices.map((p) => {
    if (p.input == null) {
      // A priced model that lost its open window — shown as a visible problem, not dropped.
      return node("div", { class: "prices-row prices-row-warn" }, [
        node("span", { class: "prices-model", text: p.model }),
        node("span", { class: "prices-warn", text: "no current price" }),
      ]);
    }
    return node("div", { class: "prices-row" }, [
      node("span", { class: "prices-model", text: p.model }),
      fieldCell("input", p.input),
      fieldCell("output", p.output),
    ]);
  });
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
