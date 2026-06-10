// Builds the filter rail from a lane's /api/filter-options payload and reads the
// rail's current state back into a filters object for /api/library. The option
// lists are lane-scoped (the caller re-renders on every run/lane change), so the
// rail only ever offers values present in the current board.

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) node.appendChild(c);
  return node;
}

// A checkbox-list group (multiselect). `values` is [{value, label}].
function checkboxGroup(legend, name, values) {
  const items = values.map(({ value, label }) => {
    const id = `f-${name}-${value}`.replace(/[^a-zA-Z0-9_-]/g, "_");
    const input = el("input", { type: "checkbox", name, value, id });
    const lab = el("label", { class: "check", for: id }, [
      input,
      el("span", { text: label }),
    ]);
    return lab;
  });
  const body = el("div", { class: "check-list" }, items);
  return el("fieldset", { class: "filter-group" }, [
    el("legend", { text: legend }),
    values.length ? body : el("p", { class: "filter-empty", text: "None in this lane" }),
  ]);
}

// A two-input numeric range. `bounds` seeds the placeholders.
function rangeGroup(legend, minId, maxId, bounds, step) {
  const lo = el("input", {
    type: "number", id: minId, class: "range-input", inputmode: "decimal",
    placeholder: bounds && bounds.min != null ? `min ${bounds.min}` : "min",
    "aria-label": `${legend} minimum`,
  });
  const hi = el("input", {
    type: "number", id: maxId, class: "range-input", inputmode: "decimal",
    placeholder: bounds && bounds.max != null ? `max ${bounds.max}` : "max",
    "aria-label": `${legend} maximum`,
  });
  if (step) { lo.setAttribute("step", step); hi.setAttribute("step", step); }
  return el("fieldset", { class: "filter-group" }, [
    el("legend", { text: legend }),
    el("div", { class: "range-row" }, [lo, hi]),
  ]);
}

// A two-input date window. Bounds (ISO timestamps) seed the date input min/max.
function dateGroup(legend, afterId, beforeId, bounds) {
  const toDate = (s) => (s && /^\d{4}-\d{2}-\d{2}/.test(s) ? s.slice(0, 10) : null);
  const after = el("input", { type: "date", id: afterId, class: "range-input",
    "aria-label": `${legend} from` });
  const before = el("input", { type: "date", id: beforeId, class: "range-input",
    "aria-label": `${legend} to` });
  if (bounds) {
    const lo = toDate(bounds.min), hi = toDate(bounds.max);
    if (lo) { after.min = lo; before.min = lo; }
    if (hi) { after.max = hi; before.max = hi; }
  }
  return el("fieldset", { class: "filter-group" }, [
    el("legend", { text: legend }),
    el("div", { class: "range-row" }, [after, before]),
  ]);
}

function toggle(legend, id) {
  const input = el("input", { type: "checkbox", id });
  return el("label", { class: "check toggle", for: id }, [
    input, el("span", { text: legend }),
  ]);
}

// Render the whole rail into `container` from an options payload.
export function renderFilterRail(container, options) {
  container.replaceChildren();

  container.appendChild(checkboxGroup(
    "Matched queries", "matched_query",
    (options.matched_queries || []).map((q) => ({ value: q, label: q })),
  ));
  container.appendChild(checkboxGroup(
    "Channel", "channel_id",
    (options.channels || []).map((c) => ({
      value: c.channel_id, label: c.channel_title || c.channel_id,
    })),
  ));
  container.appendChild(checkboxGroup(
    "Country", "country",
    (options.countries || []).map((c) => ({ value: c, label: c })),
  ));
  container.appendChild(checkboxGroup(
    "Duration", "duration_band",
    (options.duration_bands || []).map((b) => ({ value: b.key, label: b.label })),
  ));

  container.appendChild(rangeGroup(
    "Views", "view-min", "view-max", options.view_count, "1"));
  container.appendChild(rangeGroup(
    "Views-to-subs ratio", "ratio-min", "ratio-max",
    options.views_to_subs_ratio, "0.1"));
  container.appendChild(dateGroup(
    "Published", "published-after", "published-before", options.published_at));
  container.appendChild(dateGroup(
    "First seen", "first-seen-after", "first-seen-before", options.first_seen_at));

  container.appendChild(el("fieldset", { class: "filter-group toggles" }, [
    el("legend", { text: "Flags" }),
    toggle("Starred only", "starred-only"),
    toggle("Has notes only", "has-notes-only"),
  ]));
}

// Read the rail back into a filters object for api.getLibrary.
export function readFilters(container) {
  const multi = (name) =>
    [...container.querySelectorAll(`input[name="${name}"]:checked`)].map((e) => e.value);
  const val = (id) => {
    const node = container.querySelector(`#${id}`);
    return node && node.value !== "" ? node.value : null;
  };
  const checked = (id) => {
    const node = container.querySelector(`#${id}`);
    return !!(node && node.checked);
  };
  return {
    matched_query: multi("matched_query"),
    channel_id: multi("channel_id"),
    country: multi("country"),
    duration_band: multi("duration_band"),
    view_min: val("view-min"),
    view_max: val("view-max"),
    ratio_min: val("ratio-min"),
    ratio_max: val("ratio-max"),
    published_after: val("published-after"),
    published_before: val("published-before"),
    first_seen_after: val("first-seen-after"),
    first_seen_before: val("first-seen-before"),
    starred_only: checked("starred-only"),
    has_notes_only: checked("has-notes-only"),
  };
}
