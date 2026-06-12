// The model + price registry editor (/models). A master-detail page: the models
// grid on top; selecting a model reveals its price-window history + an add-price
// form below. Hand-built with the node() idiom (matches board.js / interpretation.js)
// — no grid library. The server is authoritative for validation; this surfaces its
// `detail` inline (e.g. "valid_from must be after the latest price window's start").
//
// Forward-use edits (enable/flags/delete) read from the registry; pricing is
// unaffected by soft-delete (spend keeps pricing history), so delete is reversible.

import * as api from "./api.js";

let el = null;                 // the page mount (<main id="models">)
let selected = null;           // the model whose price detail is open
let showDeletedModels = false;
let showDeletedPrices = false;
let animateNext = false;       // stagger the reveal on page-entry ONLY, not on
                               // in-page re-renders (select/add/delete) — those
                               // would re-animate the whole grid on every click.

export function init(mount) {
  el = mount;
  // One delegated listener set on the stable mount; rows are re-rendered freely.
  el.addEventListener("click", onClick);
  el.addEventListener("change", onChange);
}

// textContent only (no innerHTML); supports inputs via value/checked/disabled.
function node(tag, opts = {}, children = []) {
  const n = document.createElement(tag);
  if (opts.class) n.className = opts.class;
  if (opts.text != null) n.textContent = opts.text;
  if (opts.type != null) n.type = opts.type;
  if (opts.value != null) n.value = opts.value;
  if (opts.checked != null) n.checked = opts.checked;
  if (opts.disabled) n.disabled = true;
  if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) n.setAttribute(k, v);
  for (const c of children) n.appendChild(c);
  return n;
}

const providerOf = (model) => model.split(":")[0];

function chip(model) {
  const provider = providerOf(model);
  const modelId = model.slice(provider.length + 1) || model;
  return node("span", { class: "models-id" }, [
    node("span", { class: "models-chip", text: provider,
                   attrs: { "data-provider": provider } }),
    node("span", { class: "models-id-name", text: modelId }),
  ]);
}

const FLAG_LETTER = {
  enabled: "E", supports_temperature: "T", supports_seed: "S", is_reasoning: "R",
};
const FLAG_TITLE = {
  enabled: "enabled (offered in the dropdown)",
  supports_temperature: "supports temperature",
  supports_seed: "supports seed",
  is_reasoning: "reasoning model (informational)",
};

// A self-labeling on/off pill: the native checkbox drives a lettered pill (E/T/S/R)
// that fills when on, so a row's capability state reads at a glance.
function flagBox(field, value) {
  return node("label", { class: "models-flag", attrs: { title: FLAG_TITLE[field] } }, [
    node("input", { type: "checkbox", checked: value === 1,
                    attrs: { "data-flag": field, "aria-label": FLAG_TITLE[field] } }),
    node("span", { class: "models-flag-pill", text: FLAG_LETTER[field] }),
  ]);
}

// --- models grid ------------------------------------------------------------

function modelRow(m) {
  const flags = node("div", { class: "models-flags" }, [
    flagBox("enabled", m.enabled),
    flagBox("supports_temperature", m.supports_temperature),
    flagBox("supports_seed", m.supports_seed),
    flagBox("is_reasoning", m.is_reasoning),
  ]);
  const notes = node("input", {
    class: "range-input models-notes", value: m.notes || "",
    attrs: { "data-notes": "1", placeholder: "notes", "aria-label": "Notes" },
  });
  const action = m.deleted
    ? node("button", { class: "btn-secondary models-act", text: "Restore",
                       attrs: { "data-restore-model": m.model } })
    : node("button", { class: "btn-secondary models-act", text: "Delete",
                       attrs: { "data-delete-model": m.model } });

  const cls = "models-row" + (m.deleted ? " is-deleted" : "")
    + (m.model === selected ? " is-selected" : "");
  return node("article", {
    class: cls,
    attrs: { "data-model": m.model, "data-row": "1", tabindex: "0",
             role: "button", "aria-pressed": m.model === selected ? "true" : "false" },
  }, [
    chip(m.model),
    flags,
    notes,
    action,
  ]);
}

function newFlagLbl(field, label, checked) {
  return node("label", { class: "models-flag-lbl" }, [
    node("input", { type: "checkbox", checked,
                    attrs: { "data-new-flag": field } }),
    node("span", { text: label }),
  ]);
}

// `providers` is the backend's SUPPORTED_PROVIDERS (served by GET /api/models), so
// the picker has ONE source of truth — never a hardcoded list here. The user picks
// a provider and types just the model id; a pasted full "provider:model" also works
// (handled in the submit). All four flags (E·T·S·R) match the grid.
function addModelForm(providers) {
  const providerSelect = node("select", {
    class: "interpret-select models-add-provider",
    attrs: { "data-new-provider": "1", "aria-label": "Provider" },
  }, (providers || []).map(
    (p) => node("option", { text: p, attrs: { value: p } })));
  return node("form", { class: "models-add", attrs: { "data-add-model": "1" } }, [
    providerSelect,
    node("input", { class: "range-input models-add-id",
      attrs: { "data-new-model": "1", placeholder: "model name, e.g. claude-opus-4-6",
               "aria-label": "Model name", required: "required" } }),
    newFlagLbl("enabled", "enabled", true),
    newFlagLbl("supports_temperature", "temp", true),
    newFlagLbl("supports_seed", "seed", true),
    newFlagLbl("is_reasoning", "reasoning", false),
    node("button", { class: "btn-primary models-act", text: "Add model",
                     attrs: { type: "submit" } }),
    node("p", { class: "models-add-error", attrs: { role: "alert", "data-add-error": "1" } }),
  ]);
}

function gridHeader() {
  const labels = ["Model", "E · T · S · R", "Notes", ""];
  return node("div", { class: "models-row models-grid-head" },
    labels.map((t) => node("span", { class: "models-h", text: t })));
}

async function renderGrid() {
  let data;
  try {
    data = await api.getModels(showDeletedModels);
  } catch (err) {
    el.replaceChildren(errorState(`Could not load models: ${err.message || err}`));
    return;
  }
  const rows = data.models || [];
  const animate = animateNext;
  animateNext = false;
  const grid = node("section", { class: "glass-panel models-grid" }, [
    gridHeader(),
    ...rows.map((m, i) => {
      const r = modelRow(m);
      if (animate) {                            // staggered reveal, page-entry only
        r.classList.add("reveal");
        r.style.setProperty("--i", i);
      }
      return r;
    }),
    addModelForm(data.providers),
  ]);

  const head = node("header", { class: "models-page-head" }, [
    node("h2", { class: "heading-sm", text: "Model registry" }),
    node("label", { class: "check models-show-deleted" }, [
      node("input", { type: "checkbox", checked: showDeletedModels,
                      attrs: { "data-show-deleted-models": "1" } }),
      node("span", { text: "Show deleted" }),
    ]),
  ]);

  const detail = node("div", { class: "models-detail", attrs: { "data-detail": "1" } });
  el.replaceChildren(node("div", { class: "models-page" }, [head, grid, detail]));
  if (selected) await renderDetail();
}

function errorState(message) {
  return node("div", { class: "empty-state empty-error" }, [
    node("p", { class: "heading-sm", text: "Something went wrong." }),
    node("p", { text: message }),
  ]);
}

// --- price detail (per selected model) --------------------------------------

function priceRow(p) {
  const open = p.valid_to == null;
  const to = open
    ? node("span", { class: "models-badge", text: "current" })
    : node("span", { class: "models-mono", text: p.valid_to });
  const action = p.deleted
    ? node("button", { class: "btn-secondary models-act", text: "Restore",
                       attrs: { "data-restore-price": p.id } })
    : node("button", { class: "btn-secondary models-act", text: "Delete",
                       attrs: { "data-delete-price": p.id } });
  return node("div", { class: "models-prow" + (p.deleted ? " is-deleted" : "") }, [
    node("span", { class: "models-mono", text: p.valid_from }),
    to,
    node("span", { class: "models-mono models-num", text: Number(p.input_per_1m).toFixed(2) }),
    node("span", { class: "models-mono models-num", text: Number(p.output_per_1m).toFixed(2) }),
    action,
  ]);
}

function addPriceForm() {
  return node("form", { class: "models-add models-add-price",
                        attrs: { "data-add-price": "1" } }, [
    node("input", { class: "range-input", type: "date",
      attrs: { "data-price-from": "1", "aria-label": "Valid from", required: "required" } }),
    node("input", { class: "range-input models-num", type: "number",
      attrs: { "data-price-in": "1", step: "0.01", min: "0", placeholder: "$ in/1M",
               "aria-label": "Input price per 1M", required: "required" } }),
    node("input", { class: "range-input models-num", type: "number",
      attrs: { "data-price-out": "1", step: "0.01", min: "0", placeholder: "$ out/1M",
               "aria-label": "Output price per 1M", required: "required" } }),
    node("button", { class: "btn-primary models-act", text: "Add price",
                     attrs: { type: "submit" } }),
    node("p", { class: "models-add-error", attrs: { role: "alert", "data-price-error": "1" } }),
  ]);
}

async function renderDetail() {
  const mount = el.querySelector("[data-detail]");
  if (!mount) return;
  let data;
  try {
    data = await api.getPrices(selected, showDeletedPrices);
  } catch (err) {
    mount.replaceChildren(errorState(`Could not load prices: ${err.message || err}`));
    return;
  }
  const rows = data.prices || [];
  const head = node("header", { class: "models-detail-head" }, [
    node("div", { class: "models-detail-title" }, [
      node("span", { class: "heading-sm", text: "Prices · " }), chip(selected)]),
    node("label", { class: "check models-show-deleted" }, [
      node("input", { type: "checkbox", checked: showDeletedPrices,
                      attrs: { "data-show-deleted-prices": "1" } }),
      node("span", { text: "Show deleted" }),
    ]),
  ]);
  const grid = node("section", { class: "glass-panel models-pgrid" }, [
    node("div", { class: "models-prow models-pgrid-head" },
      ["From", "To", "$ in/1M", "$ out/1M", ""].map(
        (t) => node("span", { class: "models-h", text: t }))),
    ...(rows.length
      ? rows.map(priceRow)
      : [node("p", { class: "filter-empty", text: "No price windows yet — add one below." })]),
    addPriceForm(),
  ]);
  mount.replaceChildren(node("section", { class: "glass-panel models-detail-panel" },
    [head, grid]));
}

// --- inline delete confirmation (the one danger affordance) -----------------

async function confirmModelDelete(model, btn) {
  const row = btn.closest(".models-row");
  if (!row || row.querySelector(".models-confirm")) return;
  let count = "…";
  try {
    count = (await api.getModelInvocationCount(model)).count;
  } catch (_) { count = "?"; }
  const strip = node("div", { class: "models-confirm glass-panel", attrs: { "data-confirm": "1" } }, [
    node("p", { class: "models-confirm-msg",
      text: `${count} logged run(s). Past spend stays priced — this only hides `
            + `${model} from the dropdown. Reversible.` }),
    node("div", { class: "models-confirm-actions" }, [
      node("button", { class: "btn-secondary models-act",
                       text: "Cancel", attrs: { "data-cancel-confirm": "1" } }),
      node("button", { class: "btn-primary models-danger models-act",
                       text: "Delete", attrs: { "data-confirm-delete-model": model } }),
    ]),
  ]);
  row.appendChild(strip);
}

// --- events -----------------------------------------------------------------

function readModelRow(row) {
  const flag = (f) => row.querySelector(`[data-flag="${f}"]`).checked;
  return {
    enabled: flag("enabled"),
    supports_temperature: flag("supports_temperature"),
    supports_seed: flag("supports_seed"),
    is_reasoning: flag("is_reasoning"),
    notes: row.querySelector("[data-notes]").value,
  };
}

async function saveModelRow(row) {
  const model = row.dataset.model;
  try {
    await api.updateModel(model, readModelRow(row));
    row.classList.remove("is-error");
  } catch (err) {
    row.classList.add("is-error");
    row.title = `Could not save: ${err.message || err}`;
  }
}

async function onChange(e) {
  // A flag checkbox or the notes input (inputs fire change on blur) -> save the row.
  const editable = e.target.closest("[data-flag], [data-notes]");
  if (editable && editable.closest(".models-row")) {
    return void saveModelRow(editable.closest(".models-row"));
  }
  if (e.target.closest("[data-show-deleted-models]")) {
    showDeletedModels = e.target.checked;
    return void renderGrid();
  }
  if (e.target.closest("[data-show-deleted-prices]")) {
    showDeletedPrices = e.target.checked;
    return void renderDetail();
  }
}

async function onClick(e) {
  const t = e.target;

  const del = t.closest("[data-delete-model]");
  if (del) return void confirmModelDelete(del.getAttribute("data-delete-model"), del);

  const cancel = t.closest("[data-cancel-confirm]");
  if (cancel) { const s = t.closest("[data-confirm]"); if (s) s.remove(); return; }

  const confirmDel = t.closest("[data-confirm-delete-model]");
  if (confirmDel) {
    const model = confirmDel.getAttribute("data-confirm-delete-model");
    try { await api.deleteModel(model); if (selected === model) selected = null; }
    catch (_) { /* leave the strip; row keeps its title error */ }
    return void renderGrid();
  }

  const restore = t.closest("[data-restore-model]");
  if (restore) {
    try { await api.restoreModel(restore.getAttribute("data-restore-model")); }
    catch (_) {}
    return void renderGrid();
  }

  const delPrice = t.closest("[data-delete-price]");
  if (delPrice) {
    try { await api.deletePrice(Number(delPrice.getAttribute("data-delete-price"))); }
    catch (_) {}
    return void renderDetail();
  }

  const restorePrice = t.closest("[data-restore-price]");
  if (restorePrice) {
    try { await api.restorePrice(Number(restorePrice.getAttribute("data-restore-price"))); }
    catch (_) {}
    return void renderDetail();
  }

  // Selecting a model row (ignore clicks on its inner controls).
  const row = t.closest("[data-row]");
  if (row && !t.closest("input, button, label, form")) {
    selected = row.dataset.model;
    return void renderGrid();
  }
}

// Add-model / add-price submit (forms fire submit; delegate at the page level).
function wireSubmit() {
  el.addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    if (form.matches("[data-add-model]")) {
      const name = form.querySelector("[data-new-model]").value.trim();
      const provider = form.querySelector("[data-new-provider]").value;
      // Accept a pasted full "provider:model"; otherwise compose from the picker.
      const model = name.includes(":") ? name : `${provider}:${name}`;
      const errEl = form.querySelector("[data-add-error]");
      errEl.textContent = "";
      const flag = (f) => form.querySelector(`[data-new-flag="${f}"]`).checked;
      try {
        await api.insertModel({
          model,
          enabled: flag("enabled"),
          supports_temperature: flag("supports_temperature"),
          supports_seed: flag("supports_seed"),
          is_reasoning: flag("is_reasoning"),
        });
        selected = model;                       // open its (empty) price detail
        await renderGrid();
      } catch (err) {
        errEl.textContent = err.message || String(err);
      }
    } else if (form.matches("[data-add-price]")) {
      const errEl = form.querySelector("[data-price-error]");
      errEl.textContent = "";
      try {
        await api.insertPrice(selected, {
          valid_from: form.querySelector("[data-price-from]").value,
          input_per_1m: Number(form.querySelector("[data-price-in]").value),
          output_per_1m: Number(form.querySelector("[data-price-out]").value),
        });
        await renderDetail();
      } catch (err) {
        errEl.textContent = err.message || String(err);
      }
    }
  });
}

let wired = false;
export function refresh() {
  if (!wired) { wireSubmit(); wired = true; }
  animateNext = true;       // entrance stagger only when the page is (re)entered
  renderGrid();
}
