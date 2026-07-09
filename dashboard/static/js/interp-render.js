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
export function renderInterpretationBody(container, data, spec) {
  const raw = data && data.text;
  let parsed = null;
  try { parsed = JSON.parse(raw); } catch { parsed = null; }
  if (!parsed || typeof parsed !== "object" || !parsed.sections) {
    container.replaceChildren(node("div", { class: "interpretation-text", text: raw || "" }));
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

  // One labeled block per section, in the server's canonical order. A sentinel (or a
  // missing) value renders a muted "no data" line, kept (not hidden) so the reader sees
  // which sections had nothing rather than a silently shorter panel.
  const sections = parsed.sections || {};
  for (const { key, label } of labels) {
    const value = sections[key];
    const empty = value == null || value === noData;
    body.appendChild(node("div", { class: "interp-section" }, [
      node("h3", { class: "interp-section-label", text: label }),
      node("p", {
        class: empty ? "interp-section-value interp-nodata" : "interp-section-value",
        text: empty ? "No data for this window." : value,
      }),
    ]));
  }

  // Recommendation: the suggestion + the sections it cited, mapped to their human labels
  // (so it reads "Growth, Engagement", not raw keys) for consistency with the list above.
  const rec = parsed.recommendation || {};
  const recBox = node("div", { class: "interp-recommendation" }, [
    node("h3", { class: "interp-section-label", text: "Recommendation" }),
  ]);
  if (!rec.suggestion || rec.suggestion === noData) {
    recBox.appendChild(node("p", { class: "interp-section-value interp-nodata", text: "No recommendation yet." }));
  } else {
    recBox.appendChild(node("p", { class: "interp-section-value", text: rec.suggestion }));
    const based = Array.isArray(rec.based_on) ? rec.based_on : [];
    if (based.length) {
      const names = based.map((k) => labelFor.get(k) || k).join(", ");
      recBox.appendChild(node("p", { class: "interp-basedon", text: `Based on: ${names}` }));
    }
  }
  body.appendChild(recBox);

  container.replaceChildren(body);
}
