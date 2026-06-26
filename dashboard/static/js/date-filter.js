// Global date filter: a Period / Specific-date toggle that replaces the old single
// run picker. This module is the SINGLE source of truth for the current filter.
// It owns the filter state, exposes getCurrentFilter() (the activation path reads
// it on page show) and dispatches a date-filter-change event (the change path).
// No other module stores filter state. main.js wires this in (Gate 1C).

export const EVENT_NAME = "date-filter-change";
export const DEFAULT_PERIOD_KEY = "latest";

// Period dropdown options, in display order. Each carries exactly one shape flag:
//   latest -> start = end = the most recent run_date (the default single-run view)
//   days   -> lower bound = today minus N days (Eastern)
//   months -> lower bound = today minus N calendar months (Eastern), day-clamped
//   all    -> no bounds (full history)
export const PERIOD_OPTIONS = [
  { key: "latest", label: "Latest run", latest: true },
  { key: "p7d", label: "Last 7 days", days: 7 },
  { key: "p14d", label: "Last 14 days", days: 14 },
  { key: "p30d", label: "Last 30 days", days: 30 },
  { key: "p3m", label: "Last 3 months", months: 3 },
  { key: "p6m", label: "Last 6 months", months: 6 },
  { key: "p12m", label: "Last 12 months", months: 12 },
  { key: "all", label: "All time", all: true },
];

const IDS = {
  control: "date-filter-control",
  modePeriod: "dfc-mode-period",
  modeSpecific: "dfc-mode-specific",
  periodSel: "dfc-period",
  runSel: "dfc-run",
};

const EASTERN = "America/New_York";

// Module-level state and the last computed filter (the source of truth).
let state = { mode: "period", periodKey: DEFAULT_PERIOD_KEY, runDate: null };
let currentFilter = null;
let allRunDatesDesc = [];

function $(id) {
  return document.getElementById(id);
}

// The Eastern YYYY-MM-DD for a given Date instant, no library.
function easternDate(instant) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: EASTERN }).format(instant);
}

// Today's date as YYYY-MM-DD in Eastern time.
function nowEastern() {
  return easternDate(new Date());
}

// Lower bound for a bounded period option, as YYYY-MM-DD (Eastern). Returns null
// for "latest"/"all" (those are resolved in buildFilter, not here). The optional
// base (defaulting to now) exists so the day-clamp can be unit-tested directly.
export function computeStartDate(option, base = new Date()) {
  if (option.all || option.latest) return null;
  const d = new Date(base.getTime());
  if (option.days) {
    d.setDate(d.getDate() - option.days);
  } else if (option.months) {
    // Clamp the day so e.g. May 31 minus 3 months lands on Feb 28/29, never rolling
    // forward into March.
    const day = d.getDate();
    d.setDate(1);
    d.setMonth(d.getMonth() - option.months);
    const lastDayOfTargetMonth = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate();
    d.setDate(Math.min(day, lastDayOfTargetMonth));
  }
  return easternDate(d);
}

function optionByKey(key) {
  return PERIOD_OPTIONS.find((o) => o.key === key) || PERIOD_OPTIONS[0];
}

// Pure: compute the filter object from a state snapshot + the DESC list of run
// dates. run dates are zero-padded ISO date keys, so string compare is correct.
export function buildFilter(st, runDatesDesc) {
  if (!runDatesDesc.length) {
    return {
      mode: st.mode,
      start_date: null,
      end_date: null,
      active_run_date: null,
      run_count: 0,
    };
  }
  const newest = runDatesDesc[0];

  if (st.mode === "specific") {
    const d = st.runDate || newest;
    return { mode: "specific", start_date: d, end_date: d, active_run_date: d, run_count: 1 };
  }

  const option = optionByKey(st.periodKey);
  if (option.latest) {
    return {
      mode: "period",
      start_date: newest,
      end_date: newest,
      active_run_date: newest,
      run_count: 1,
    };
  }
  if (option.all) {
    return {
      mode: "period",
      start_date: null,
      end_date: null,
      active_run_date: newest,
      run_count: runDatesDesc.length,
    };
  }

  // Bounded window (days / months): end is today (Eastern).
  const start = computeStartDate(option);
  const end = nowEastern();
  const inWindow = runDatesDesc.filter((d) => d >= start && d <= end);
  const active = inWindow.length ? inWindow[0] : newest;
  return {
    mode: "period",
    start_date: start,
    end_date: end,
    active_run_date: active,
    run_count: inWindow.length,
  };
}

// Pure read for the activation path: the last computed filter (null before init).
export function getCurrentFilter() {
  return currentFilter;
}

function recompute() {
  currentFilter = buildFilter(state, allRunDatesDesc);
  return currentFilter;
}

function dispatchFilter() {
  document.dispatchEvent(new CustomEvent(EVENT_NAME, { detail: currentFilter }));
}

function applyModeUI() {
  const specific = state.mode === "specific";
  const periodTab = $(IDS.modePeriod);
  const specificTab = $(IDS.modeSpecific);
  // Active segment: aria-selected drives the filled pill; roving tabindex keeps a
  // single tab stop in the segmented control (matches the lane tablist).
  periodTab.setAttribute("aria-selected", specific ? "false" : "true");
  specificTab.setAttribute("aria-selected", specific ? "true" : "false");
  periodTab.tabIndex = specific ? -1 : 0;
  specificTab.tabIndex = specific ? 0 : -1;
  $(IDS.periodSel).hidden = specific;
  $(IDS.runSel).hidden = !specific;
}

function fillSelect(select, entries, valueOf, labelOf) {
  select.replaceChildren();
  for (const e of entries) {
    const opt = document.createElement("option");
    opt.value = valueOf(e);
    opt.textContent = labelOf(e);
    select.appendChild(opt);
  }
}

// Called once from main.js (Gate 1C). Populates the control, wires its handlers,
// and computes the initial filter. Does NOT dispatch on load: the router's
// activation path renders the first visible page from getCurrentFilter().
export function initDateFilter(runDatesDesc) {
  allRunDatesDesc = runDatesDesc || [];
  state = {
    mode: "period",
    periodKey: DEFAULT_PERIOD_KEY,
    runDate: allRunDatesDesc[0] ?? null,
  };

  const periodSel = $(IDS.periodSel);
  const runSel = $(IDS.runSel);
  const periodTab = $(IDS.modePeriod);
  const specificTab = $(IDS.modeSpecific);

  fillSelect(periodSel, PERIOD_OPTIONS, (o) => o.key, (o) => o.label);
  periodSel.value = DEFAULT_PERIOD_KEY;

  fillSelect(runSel, allRunDatesDesc, (d) => d, (d) => d);
  if (allRunDatesDesc.length) runSel.value = allRunDatesDesc[0];

  applyModeUI();

  function setMode(mode) {
    if (mode === state.mode) return;
    state.mode = mode;
    applyModeUI();
    recompute();
    dispatchFilter();
  }

  // Two-segment mode control (tablist): click selects, Arrow keys rove + select.
  const modeTabs = [periodTab, specificTab];
  modeTabs.forEach((tab, i) => {
    tab.addEventListener("click", () => setMode(tab.dataset.mode));
    tab.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      const dir = e.key === "ArrowRight" ? 1 : -1;
      const next = modeTabs[(i + dir + modeTabs.length) % modeTabs.length];
      next.focus();
      setMode(next.dataset.mode);
    });
  });

  periodSel.addEventListener("change", () => {
    state.periodKey = periodSel.value;
    recompute();
    dispatchFilter();
  });

  runSel.addEventListener("change", () => {
    state.runDate = runSel.value;
    recompute();
    dispatchFilter();
  });

  recompute();
}
