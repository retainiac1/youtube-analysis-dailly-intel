// Spend panel for the Interpretation page. Shows what the LLM runs have cost: two
// breakdowns, each split by model -- "This run" (the run in the picker) and "Month to
// date" (the current Eastern calendar month). Read-only display.
//
// Each scope renders a spend-share DONUT (each priced model's slice of that scope's
// dollar total, darkest green = largest share) beside the scope total, then a per-model
// LEADERBOARD sorted by blended $/M ascending (most efficient first): a blue token bar,
// a green cost bar, and a $/M efficiency badge. All viz colors, thresholds, and donut
// radii come from settings.toml via the /api/spend `viz` block -- none are hardcoded
// here. The derived numbers (share_pct, cost_per_million) are computed server-side; this
// module only renders and rounds.
//
// The two sections deliberately use DIFFERENT time keys (run_date vs the month a
// generation was incurred in), so a run re-interpreted today shows under its old run AND
// under this month. That is by design -- see /api/spend.
//
// Tokens are EXACT (thousands-separated); dollar cost is an ESTIMATE ("est." on the
// total) and shown "unavailable" for any model absent from the price map.

/* global echarts */

import * as api from "./api.js";

let el = null;
let lastData = null;
// Live donut instances keyed by their mount element, so a window resize can re-flow
// them and each render can dispose the previous ones (ECharts cannot re-theme live).
const donutInstances = new Map();
let resizeBound = false;

export function init(mount) {
  el = mount;
  // Register the resize handler ONCE (not per render, or it would leak a listener
  // every time the panel re-renders).
  if (!resizeBound) {
    window.addEventListener("resize", () => {
      for (const inst of donutInstances.values()) inst.resize();
    });
    resizeBound = true;
  }
}

function node(tag, opts = {}, children = []) {
  const n = document.createElement(tag);
  if (opts.class) n.className = opts.class;
  if (opts.text != null) n.textContent = opts.text;
  if (opts.style) Object.assign(n.style, opts.style);
  for (const c of children) n.appendChild(c);
  return n;
}

const intFmt = new Intl.NumberFormat();
// Runs are sub-cent, so show enough precision to be non-zero (4 dp).
const usdFmt = new Intl.NumberFormat(undefined, {
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});
// Share of scope spend: a fraction rendered as a 1 dp percent.
const shareFmt = new Intl.NumberFormat(undefined, {
  style: "percent",
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

function fmtTokens(n) {
  return n == null ? "0" : intFmt.format(n);
}

function fmtCost(cost) {
  return cost == null ? "unavailable" : `$${usdFmt.format(cost)}`;
}

function fmtShare(share) {
  return share == null ? "" : shareFmt.format(share);
}

// Center-label colors follow the document theme; read the CSS tokens rather than
// duplicate hexes, so the donut text flips with light/dark like everything else.
function themeColor(name, fallback) {
  const v = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
  return v || fallback;
}

// Unpriced models sort LAST. A naive (a - b) would coerce null to 0 and rank the
// unpriced as the most efficient -- the opposite of what we want.
function byEfficiency(a, b) {
  const x = a.cost_per_million;
  const y = b.cost_per_million;
  if (x == null && y == null) return 0;
  if (x == null) return 1;
  if (y == null) return -1;
  return x - y;
}

// A bar: a full-width neutral track with a colored fill scaled to the scope max.
function bar(value, max, color) {
  const pct = max > 0 && value != null ? (value / max) * 100 : 0;
  const fill = node("div", { class: "spend-bar-fill" });
  fill.style.width = `${pct}%`;
  fill.style.background = color;
  return node("div", { class: "spend-bar-track" }, [fill]);
}

// One leaderboard row: model name, then the blue token bar and the mint cost bar each
// with their trailing value. Rows are still ordered most-efficient-first (by $/M); the
// ordering no longer carries a visible badge.
function leaderRow(m, scopeMaxTokens, scopeMaxCost, viz) {
  const tokens = m.input_tokens + m.output_tokens;

  return node("div", { class: "spend-leader-row" }, [
    node("span", { class: "spend-model", text: m.model }),
    node("div", { class: "spend-bar-line" }, [
      bar(tokens, scopeMaxTokens, viz.token_bar_color),
      node("span", { class: "spend-bar-value", text: `${fmtTokens(tokens)} tok` }),
    ]),
    node("div", { class: "spend-bar-line" }, [
      bar(m.cost, scopeMaxCost, viz.cost_bar_color),
      node("span", {
        class: m.cost == null ? "spend-bar-value spend-cost-unavailable"
                              : "spend-bar-value",
        text: fmtCost(m.cost),
      }),
    ]),
  ]);
}

// The donut's static side legend: one swatch + model + share per priced model (in the
// same share-descending order as the slices), then any unpriced models as muted rows.
function donutLegend(priced, colorOf, unpriced) {
  const rows = priced.map((m) => {
    const sw = node("span", { class: "spend-swatch" });
    sw.style.background = colorOf.get(m.model);
    return node("div", { class: "spend-legend-row" }, [
      sw,
      node("span", { class: "spend-legend-model", text: m.model }),
      node("span", { class: "spend-legend-share", text: fmtShare(m.share_pct) }),
    ]);
  });
  for (const m of unpriced) {
    rows.push(node("div", { class: "spend-legend-row spend-cost-unavailable" }, [
      node("span", { class: "spend-swatch spend-swatch-empty" }),
      node("span", { class: "spend-legend-model", text: m.model }),
      node("span", { class: "spend-legend-share", text: "unpriced" }),
    ]));
  }
  return node("div", { class: "spend-donut-legend" }, rows);
}

// Build one scope (This run / Month to date). Returns the section element and, when the
// scope has priced spend, a pending-donut descriptor to init AFTER it is in the DOM
// (echarts.init needs a sized, attached element).
function buildSection(title, data, emptyText, viz) {
  const children = [];

  const priced = data.per_model
    .filter((m) => m.cost != null)
    .sort((a, b) => (b.share_pct || 0) - (a.share_pct || 0));
  const unpriced = data.per_model.filter((m) => m.cost == null);
  const willRenderDonut = priced.length && data.total_cost > 0;

  // Header is the scope label. The dollar total is the donut's CENTER label, so it is
  // printed here only when there is no donut to carry it (empty/degenerate scope) --
  // never both, which removes the duplicated total of the old layout.
  const header = [node("span", { class: "spend-scope-title", text: title })];
  if (!willRenderDonut && data.per_model.length) {
    header.push(node("span", {
      class: "spend-scope-total",
      text: data.has_unpriced
        ? `$${usdFmt.format(data.total_cost)} est. (excludes unpriced)`
        : `$${usdFmt.format(data.total_cost)} est.`,
    }));
  }
  children.push(node("div", { class: "spend-scope-header" }, header));

  if (!data.per_model.length) {
    children.push(node("p", { class: "spend-empty", text: emptyText }));
    return { section: node("section", { class: "spend-section" }, children) };
  }

  // share-descending -> slice colors brightest first, so the largest share is the most
  // saturated (and most visible) slice.
  const colorOf = new Map();
  priced.forEach((m, i) => {
    colorOf.set(m.model, viz.donut_slice_colors[i % viz.donut_slice_colors.length]);
  });

  const donutEl = node("div", { class: "spend-donut" });
  let pending = null;
  if (willRenderDonut) {
    pending = { el: donutEl, priced, total: data.total_cost, colorOf, viz };
  } else {
    // Degenerate scope (no priced spend): a placeholder, never an empty chart.
    donutEl.classList.add("spend-donut-empty");
    donutEl.appendChild(
      node("p", { class: "spend-empty", text: "No priced spend to chart." }),
    );
  }
  children.push(node("div", { class: "spend-donut-wrap" }, [
    donutEl,
    donutLegend(priced, colorOf, unpriced),
  ]));
  if (willRenderDonut && data.has_unpriced) {
    children.push(node("p", {
      class: "spend-unpriced-note",
      text: "Total excludes unpriced models.",
    }));
  }

  // Leaderboard: every model, most efficient first, unpriced last.
  const scopeMaxTokens = Math.max(
    ...data.per_model.map((m) => m.input_tokens + m.output_tokens), 0,
  );
  const scopeMaxCost = Math.max(
    ...data.per_model.map((m) => m.cost || 0), 0,
  );
  const board = node("div", { class: "spend-leaderboard" });
  for (const m of [...data.per_model].sort(byEfficiency)) {
    board.appendChild(leaderRow(m, scopeMaxTokens, scopeMaxCost, viz));
  }
  children.push(board);

  return { section: node("section", { class: "spend-section" }, children), pending };
}

function initDonut({ el: mountEl, priced, total, colorOf, viz }) {
  const inst = echarts.init(mountEl);
  inst.setOption({
    animation: !(
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ),
    tooltip: {
      trigger: "item",
      formatter: (p) => `${p.name}<br/>$${usdFmt.format(p.value)} (${p.percent}%)`,
    },
    series: [{
      type: "pie",
      radius: [viz.donut_inner_radius, viz.donut_outer_radius],
      center: ["50%", "50%"],
      avoidLabelOverlap: false,
      label: { show: false },
      labelLine: { show: false },
      // A panel-colored stroke separates adjacent slices so same-family mint stops
      // never blur together.
      itemStyle: { borderColor: themeColor("--bg-base", "#0a0f1a"), borderWidth: 2 },
      data: priced.map((m) => ({
        name: m.model,
        value: m.cost,
        itemStyle: { color: colorOf.get(m.model) },
      })),
    }],
  });
  donutInstances.set(mountEl, inst);
  // The total sits in the ring's hole as an HTML overlay (not an ECharts title), so it
  // is exactly centered via CSS and never grazes the ring. pointer-events:none keeps
  // slice-hover tooltips working on the canvas beneath it.
  mountEl.appendChild(node("div", { class: "spend-donut-center" }, [
    node("span", { class: "spend-donut-amount", text: `$${usdFmt.format(total)}` }),
    node("span", { class: "spend-donut-est", text: "est." }),
  ]));
}

function renderError(message) {
  disposeDonuts();
  el.replaceChildren(
    node("div", { class: "spend-empty empty-error" }, [
      node("p", { class: "heading-sm", text: "Could not load spend." }),
      node("p", { text: message }),
    ]),
  );
}

function disposeDonuts() {
  for (const inst of donutInstances.values()) inst.dispose();
  donutInstances.clear();
}

// The panel-level caption, shown once (not per scope): the tokens=blue / cost=mint key.
// De-duplicates the legend the old layout repeated per scope.
function topCaption(viz) {
  const tokenSwatch = node("span", { class: "spend-swatch" });
  tokenSwatch.style.background = viz.token_bar_color;
  const costSwatch = node("span", { class: "spend-swatch" });
  costSwatch.style.background = viz.cost_bar_color;
  return node("div", { class: "spend-caption" }, [
    node("div", { class: "spend-legend" }, [
      node("span", { class: "spend-legend-item" }, [
        tokenSwatch, node("span", { text: "tokens" }),
      ]),
      node("span", { class: "spend-legend-item" }, [
        costSwatch, node("span", { text: "cost" }),
      ]),
    ]),
  ]);
}

function render(data) {
  disposeDonuts();
  const runLabel = data.run_date ? `This run · ${data.run_date}` : "This run";
  const run = buildSection(runLabel, data.run, "No runs for this run yet.", data.viz);
  const month = buildSection(
    `Month to date · ${data.month}`, data.month_to_date,
    "No runs this month yet.", data.viz,
  );
  const hasData =
    data.run.per_model.length || data.month_to_date.per_model.length;
  const children = [
    node("h2", { class: "heading-sm spend-title", text: "LLM spend" }),
  ];
  if (hasData) children.push(topCaption(data.viz));
  children.push(run.section, month.section);
  el.replaceChildren(...children);
  // Init donuts only after their mount elements are attached and sized.
  if (run.pending) initDonut(run.pending);
  if (month.pending) initDonut(month.pending);
}

// Re-render from the last fetched payload. Used by the theme toggle: ECharts cannot
// re-theme a live instance, so the donuts must be disposed and rebuilt.
export function rerenderFromCache() {
  if (lastData) render(lastData);
}

export async function refresh(state) {
  let data;
  try {
    data = await api.getSpend(state.runDate);
  } catch (err) {
    renderError(String(err.message || err));
    return;
  }
  lastData = data;
  render(data);
}
