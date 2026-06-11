// Spend panel for the Interpretation page. Shows what the LLM runs have cost: two
// breakdowns, each split by model — "This run" (the run in the picker) and "Month to
// date" (the current Eastern calendar month). Read-only display, mirroring the
// interpretation-rail structure (init/refresh, a text-only node() builder, atomic
// replaceChildren).
//
// The two sections deliberately use DIFFERENT time keys (run_date vs the month a
// generation was incurred in), so a run re-interpreted today shows under its old run
// AND under this month. That is by design — see /api/spend.
//
// Tokens are EXACT (never abbreviated); dollar cost is an ESTIMATE, suffixed "est."
// and shown "unavailable" for any model absent from the price map.

import * as api from "./api.js";

let el = null;

export function init(mount) {
  el = mount;
}

function node(tag, opts = {}, children = []) {
  const n = document.createElement(tag);
  if (opts.class) n.className = opts.class;
  if (opts.text != null) n.textContent = opts.text;
  for (const c of children) n.appendChild(c);
  return n;
}

const intFmt = new Intl.NumberFormat();
// Runs are sub-cent, so show enough precision to be non-zero (4 dp).
const usdFmt = new Intl.NumberFormat(undefined, {
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});

function fmtTokens(n) {
  return n == null ? "0" : intFmt.format(n);
}

// A priced cost -> "$0.0021 est."; a null cost -> "unavailable".
function fmtCost(cost) {
  return cost == null ? "unavailable" : `$${usdFmt.format(cost)} est.`;
}

// One model row: the canonical provider:model, the exact token split, and the cost
// (or "unavailable"). The cost class lets the muted/unavailable styling apply.
function modelRow(m) {
  const unavailable = m.cost == null;
  return node("div", { class: "spend-row" }, [
    node("span", { class: "spend-model", text: m.model }),
    node("span", {
      class: "spend-tokens",
      text: `${fmtTokens(m.input_tokens)} in / ${fmtTokens(m.output_tokens)} out`,
    }),
    node("span", {
      class: unavailable ? "spend-cost spend-cost-unavailable" : "spend-cost",
      text: fmtCost(m.cost),
    }),
  ]);
}

// One labeled section (This run / Month to date): a sub-heading, the total line, and
// a row per model. An empty section renders a one-line empty state instead of a list.
function section(title, data, emptyText) {
  const children = [node("h3", { class: "spend-section-title", text: title })];

  if (!data.per_model.length) {
    children.push(node("p", { class: "spend-empty", text: emptyText }));
    return node("section", { class: "spend-section" }, children);
  }

  // The total is the sum of PRICED models only; flag when unpriced rows are excluded
  // so the labeled total stays honest.
  const totalText = data.has_unpriced
    ? `Total: $${usdFmt.format(data.total_cost)} est. (excludes unpriced)`
    : `Total: $${usdFmt.format(data.total_cost)} est.`;
  children.push(node("p", { class: "spend-total", text: totalText }));
  for (const m of data.per_model) children.push(modelRow(m));
  return node("section", { class: "spend-section" }, children);
}

function renderError(message) {
  el.replaceChildren(
    node("div", { class: "spend-empty empty-error" }, [
      node("p", { class: "heading-sm", text: "Could not load spend." }),
      node("p", { text: message }),
    ]),
  );
}

function render(data) {
  const runLabel = data.run_date ? `This run · ${data.run_date}` : "This run";
  el.replaceChildren(
    node("h2", { class: "heading-sm spend-title", text: "LLM spend" }),
    section(runLabel, data.run, "No runs for this run yet."),
    section(`Month to date · ${data.month}`, data.month_to_date,
            "No runs this month yet."),
  );
}

export async function refresh(state) {
  let data;
  try {
    data = await api.getSpend(state.runDate);
  } catch (err) {
    renderError(String(err.message || err));
    return;
  }
  render(data);
}
