// Shared renderer for the structured interpretation contract (interpret.py's JSON). The
// stored `text` is a JSON object: {contract_version, context_mode, sections{12 keys},
// recommendation{suggestion, based_on}, optional _partial/_errors/_missing}. The
// Interpretation page card and BOTH rails (Trends + Dashboard) render through here, so the
// per-section display is defined ONCE. textContent only (LLM text is never inserted as HTML).

import * as api from "./api.js";

function node(tag, opts = {}, children = []) {
  const n = document.createElement(tag);
  if (opts.class) n.className = opts.class;
  if (opts.text != null) n.textContent = opts.text;
  for (const c of children) if (c) n.appendChild(c);
  return n;
}

// Lazily fetch + cache the server-owned label/sentinel spec (ONE fetch, shared by every
// consumer), so the section labels and the empty-section sentinel have a single source and
// cannot drift from interpret.py. On a defaults-fetch failure the renderer still works: it
// falls back to raw keys and no sentinel (every section then renders its value verbatim).
let specPromise = null;
export function loadSpec() {
  if (!specPromise) {
    specPromise = api.getInterpretDefaults()
      .then((d) => ({ sectionLabels: d.section_labels || [], noData: d.no_data }))
      .catch(() => ({ sectionLabels: [], noData: null }));
  }
  return specPromise;
}

// Render the parsed contract into `container` as a readable per-section list + the
// recommendation. `data` is the interpretation read payload (data.text is the JSON string);
// `spec` = {sectionLabels: [{key,label}], noData}. Never throws: a non-JSON / non-contract
// text falls back to a plain-string render (legacy or degraded rows).
// Parse the stored contract text; returns the object or null (non-JSON / non-contract).
function parseContract(data) {
  const raw = data && data.text;
  let parsed = null;
  try { parsed = JSON.parse(raw); } catch { parsed = null; }
  if (!parsed || typeof parsed !== "object" || !parsed.sections) return null;
  return parsed;
}

// One labeled section block: label + value, or a muted "no data" line for a sentinel/missing
// value (kept, not hidden, so coverage is visible). Shared by the whole-body + single-section
// renders so both look identical.
function sectionEl(label, value, noData) {
  const empty = value == null || value === noData;
  return node("div", { class: "interp-section" }, [
    node("h3", { class: "interp-section-label", text: label }),
    node("p", {
      class: empty ? "interp-section-value interp-nodata" : "interp-section-value",
      text: empty ? "No data for this window." : value,
    }),
  ]);
}

// The recommendation block: the suggestion + the sections it cited, mapped to human labels
// (so it reads "Growth, Engagement", not raw keys).
function recommendationEl(rec, labelFor, noData) {
  const box = node("div", { class: "interp-recommendation" }, [
    node("h3", { class: "interp-section-label", text: "Recommendation" }),
  ]);
  if (!rec || !rec.suggestion || rec.suggestion === noData) {
    box.appendChild(node("p", { class: "interp-section-value interp-nodata", text: "No recommendation yet." }));
    return box;
  }
  box.appendChild(node("p", { class: "interp-section-value", text: rec.suggestion }));
  const based = Array.isArray(rec.based_on) ? rec.based_on : [];
  if (based.length) {
    const names = based.map((k) => labelFor.get(k) || k).join(", ");
    box.appendChild(node("p", { class: "interp-basedon", text: `Based on: ${names}` }));
  }
  return box;
}

export function renderInterpretationBody(container, data, spec) {
  const parsed = parseContract(data);
  if (!parsed) {
    container.replaceChildren(node("div", { class: "interpretation-text", text: (data && data.text) || "" }));
    return;
  }
  const labels = (spec && spec.sectionLabels) || [];
  const noData = spec && spec.noData;
  const labelFor = new Map(labels.map((l) => [l.key, l.label]));
  const body = node("div", { class: "interp-body" });

  if (parsed._partial) {
    body.appendChild(node("p", {
      class: "interp-partial",
      text: "Partial response: some sections could not be parsed and are shown as no data.",
    }));
  }
  const sections = parsed.sections || {};
  for (const { key, label } of labels) {
    body.appendChild(sectionEl(label, sections[key], noData));
  }
  body.appendChild(recommendationEl(parsed.recommendation, labelFor, noData));
  container.replaceChildren(body);
}

// Single-section render for the scroll-synced Dashboard rail: shows just the section matching
// the chart in view, plus the recommendation (always reachable). DEGRADES to the whole body
// when the key is absent/empty, the text is non-contract, or there are no labels, so the rail
// is never blank or misleading.
export function renderInterpretationSection(container, data, spec, key) {
  const parsed = parseContract(data);
  const labels = (spec && spec.sectionLabels) || [];
  const noData = spec && spec.noData;
  const labelFor = new Map(labels.map((l) => [l.key, l.label]));
  const value = parsed && parsed.sections ? parsed.sections[key] : undefined;
  const label = labelFor.get(key);
  // Degrade to the whole panel when we cannot show a real single section.
  if (!parsed || !label || value == null || value === noData) {
    renderInterpretationBody(container, data, spec);
    return;
  }
  const body = node("div", { class: "interp-body interp-body-single" }, [
    sectionEl(label, value, noData),
    recommendationEl(parsed.recommendation, labelFor, noData),
  ]);
  container.replaceChildren(body);
}
