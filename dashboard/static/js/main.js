// Bootstrap: load runs + lane options, render the board, and wire the run
// picker, page nav (router), lane tabs, filter rail (debounced), theme toggle,
// and mobile drawer. Board, Trends, and Interpretation are URL-routed pages
// (/board, /trends, /interpretation); the router swaps which page is shown, while
// the run and lane persist across all of them.

import * as api from "./api.js";
import * as filters from "./filters.js";
import * as board from "./board.js";
import * as trends from "./trends.js";
import * as interpretation from "./interpretation.js";
import * as extract from "./extract.js";
import * as railModule from "./interpretation-rail.js";
import * as spend from "./spend.js";
import * as models from "./models.js";
import * as priceRefresh from "./price-refresh.js";
import * as documentation from "./documentation.js";
import * as router from "./router.js";
import { initDateFilter, getCurrentFilter, EVENT_NAME } from "./date-filter.js";
import * as dashboard from "./dashboard.js";
import "./sticky-header.js"; // side-effect: publishes --header-h for the pinned strip

const app = document.getElementById("app");
const dateFilterControl = document.getElementById("date-filter-control");
const boardEl = document.getElementById("board");
const trendsEl = document.getElementById("trends");
const interpretationEl = document.getElementById("interpretation");
const extractEl = document.getElementById("extract");
const modelsEl = document.getElementById("models");
const pricesEl = document.getElementById("prices");
const documentationEl = document.getElementById("documentation");
const dashboardEl = document.getElementById("dashboard");
const railGroups = document.getElementById("filter-groups");
const rail = document.getElementById("filter-rail");
const drawerToggle = document.getElementById("filter-drawer-toggle");
const laneRow = document.querySelector(".lane-row");
const tabs = [...document.querySelectorAll(".lane-tab")];
const pageNav = [...document.querySelectorAll(".page-nav-link")];

const MAX_TRACKED = 5;
const BOARD_ROUTE = "/board";
const TRENDS_ROUTE = "/trends";
const INTERPRETATION_ROUTE = "/interpretation";
const PRICES_ROUTE = "/prices";
const EXTRACT_ROUTE = "/extract";
const DASHBOARD_ROUTE = "/dashboard";

// Mount container ids for the shared spend panel and the Prices content column.
// Named here (not sprinkled as literals) so each id has one source; the spend panel
// is created once per mount via spend.create (see init).
const INTERP_SPEND_ID = "spend-panel";
const PRICES_SPEND_ID = "prices-spend-panel";
const PRICES_MAIN_ID = "prices-main";
const EXTRACT_SPEND_ID = "extract-spend-panel";

// One independent spend-panel instance per page, created in init(). Each owns its own
// data cache + donut instances + resize listener, so they never collide.
let interpSpend = null;
let pricesSpend = null;
let extractSpend = null;
// Two interpretation-rail instances (the rail is a factory): one on Trends, one on the
// Dashboard. Created in init() once their mount elements exist.
let trendsRail = null;
let dashboardRail = null;

// The active page is owned by the router (router.current()); run/lane/tracked are
// shared state that persists across pages. tracked is EPHEMERAL in-memory view
// state (which videos are charted), distinct from starred (a Phase 3 DB write).
const state = { runDate: null, startDate: null, endDate: null, lane: "health", tracked: new Set() };

function trackState() {
  return { tracked: state.tracked, full: state.tracked.size >= MAX_TRACKED };
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

async function reloadOptions() {
  const options = await api.getFilterOptions(state.runDate, state.lane);
  filters.renderFilterRail(railGroups, options);
}

async function reloadBoard() {
  try {
    const data = await api.getLibrary(
      state.runDate, state.lane, filters.readFilters(railGroups));
    board.renderRows(boardEl, data, state.lane, trackState());
  } catch (err) {
    board.renderError(boardEl, String(err.message || err));
  }
}

const debouncedReload = debounce(reloadBoard, 250);

async function onRunOrLaneChange() {
  // Options are lane+run scoped, so refetch them, then reload the board.
  await reloadOptions();
  await reloadBoard();
}

// A lane change or a date-filter change re-renders all Trends charts: the
// histogram is keyed on the active run, while the bump chart and trajectory window
// to the selected date range, so all three move together. The interpretation rail
// (summary keyed on run + lane) refreshes alongside them.
async function refreshTrendsCharts() {
  await trends.refreshAll(state);
  trendsRail.refresh(state);
}

// The Dashboard charts + its interpretation rail always refresh together (the rail is
// window+lane keyed, so it must not lag the charts). One helper so every lane/window change
// pairs them; see the three call sites (selectLane, enterDashboard, the date-filter listener).
function refreshDashboardAll() {
  dashboard.refresh(state);
  dashboardRail.refresh(state);
}

function selectLane(lane) {
  if (lane === state.lane) return;
  state.lane = lane;
  board.setActiveTab(app, tabs, lane);
  onRunOrLaneChange();
  if (router.current() === TRENDS_ROUTE) refreshTrendsCharts();
  if (router.current() === INTERPRETATION_ROUTE) interpretation.refresh(state);
  if (router.current() === DASHBOARD_ROUTE) refreshDashboardAll();
}

// Route handlers: show the page's content region and hide the others, and show or
// hide the date filter per page. Board, Trends, and Interpretation are date-scoped
// (filter visible); Extract, Models, Prices, and Documentation are not (hidden).
// Each date-scoped page re-renders from the current filter on entry (activation
// path), so a filter change made on another page is reflected when you return.
function enterBoard() {
  boardEl.hidden = false;
  rail.hidden = false;
  drawerToggle.hidden = false;
  trendsEl.hidden = true;
  interpretationEl.hidden = true;
  extractEl.hidden = true;
  modelsEl.hidden = true;
  documentationEl.hidden = true;
  dashboardEl.hidden = true;
  pricesEl.hidden = true;
  laneRow.hidden = false;
  dateFilterControl.hidden = false;
  // Activation path: re-apply the current filter on entry (options + rows). The
  // date-filter-change listener guards its Board reload on BOARD_ROUTE, so a filter
  // change (which never navigates) and this entry render never double-fetch.
  onRunOrLaneChange();
}

function enterTrends() {
  boardEl.hidden = true;
  rail.hidden = true;
  drawerToggle.hidden = true;
  trendsEl.hidden = false;
  interpretationEl.hidden = true;
  extractEl.hidden = true;
  modelsEl.hidden = true;
  documentationEl.hidden = true;
  dashboardEl.hidden = true;
  pricesEl.hidden = true;
  laneRow.hidden = false;
  dateFilterControl.hidden = false;
  // Render the charts on entry: an ECharts instance on a hidden element cannot
  // size itself, so charts are only rendered while Trends is visible.
  trends.refreshAll(state);
  // The read-only interpretation rail beside the charts (window + lane scoped).
  trendsRail.refresh(state);
}

function enterInterpretation() {
  boardEl.hidden = true;
  rail.hidden = true;
  drawerToggle.hidden = true;
  trendsEl.hidden = true;
  interpretationEl.hidden = false;
  extractEl.hidden = true;
  modelsEl.hidden = true;
  documentationEl.hidden = true;
  dashboardEl.hidden = true;
  pricesEl.hidden = true;
  laneRow.hidden = false;
  dateFilterControl.hidden = false;
  // Fetch + render the interpretation for the current run + lane on entry.
  interpretation.refresh(state);
  // The spend panel beside it (run section + month-to-date), refreshed on entry.
  interpSpend.refresh(state);
}

// The Extract page: run a discovery from the UI. A global action surface (not run/lane
// scoped), so it hides the filter rail/drawer and the lane tabs like Prices. Phase 2 only
// builds the shell; the SSE stream wiring (Phase 3) and the spend panel (Phase 5) arrive
// later, so entry just ensures the scaffold exists.
function enterExtract() {
  boardEl.hidden = true;
  rail.hidden = true;
  drawerToggle.hidden = true;
  trendsEl.hidden = true;
  interpretationEl.hidden = true;
  extractEl.hidden = false;
  modelsEl.hidden = true;
  documentationEl.hidden = true;
  dashboardEl.hidden = true;
  pricesEl.hidden = true;
  laneRow.hidden = true; // not lane-scoped; hide the lane tabs here
  dateFilterControl.hidden = true; // not date-scoped; hide the date filter here
  extract.refresh(state);
  // The same spend panel as Interpretation/Prices, in this page's right aside (its own
  // instance), refreshed on entry. Re-entering after a background run completed catches up
  // the panel here (the extract:completed listener only refreshes while the page is shown).
  extractSpend.refresh(state);
}

// The registry editor: a global admin page (not run/lane scoped). Hide every other
// region + the filter rail/drawer, then load the models grid.
function enterModels() {
  boardEl.hidden = true;
  rail.hidden = true;
  drawerToggle.hidden = true;
  trendsEl.hidden = true;
  interpretationEl.hidden = true;
  extractEl.hidden = true;
  modelsEl.hidden = false;
  documentationEl.hidden = true;
  dashboardEl.hidden = true;
  pricesEl.hidden = true;
  laneRow.hidden = false;
  dateFilterControl.hidden = true; // not date-scoped; hide the date filter here
  models.refresh();
}

// The documentation browser: a read-only, filesystem-discovered docs page. Not
// run/lane scoped, so (like Models) it hides the filter rail/drawer and ignores the
// lane. documentation.js fetches the registry and renders into the region on entry.
function enterDocumentation() {
  boardEl.hidden = true;
  rail.hidden = true;
  drawerToggle.hidden = true;
  trendsEl.hidden = true;
  interpretationEl.hidden = true;
  extractEl.hidden = true;
  modelsEl.hidden = true;
  pricesEl.hidden = true;
  documentationEl.hidden = false;
  dashboardEl.hidden = true;
  laneRow.hidden = true; // the page is not lane-scoped; hide the lane tabs here
  dateFilterControl.hidden = true; // not date-scoped; hide the date filter here
  documentation.refresh();
}

// The price-refresh agent: a global admin page (not run/lane scoped), like Models. Hide
// every other region + the filter rail/drawer, then load the prices + proposals.
function enterPrices() {
  boardEl.hidden = true;
  rail.hidden = true;
  drawerToggle.hidden = true;
  trendsEl.hidden = true;
  interpretationEl.hidden = true;
  extractEl.hidden = true;
  modelsEl.hidden = true;
  documentationEl.hidden = true;
  dashboardEl.hidden = true;
  pricesEl.hidden = false;
  laneRow.hidden = true; // not lane-scoped; hide the lane tabs here
  dateFilterControl.hidden = true; // not date-scoped; hide the date filter here
  priceRefresh.refresh();
  // The same spend panel as Interpretation, in this page's right aside (its own
  // instance), refreshed on entry. One fetch per entry: the date-filter change
  // handler only fires on a real change, never on route entry.
  pricesSpend.refresh(state);
}

// The Dashboard page: lane-scoped, period-aggregated charts across four sub-tabs. Like
// Trends, it shows the lane tabs and the date filter; dashboard.js renders on entry.
function enterDashboard() {
  boardEl.hidden = true;
  rail.hidden = true;
  drawerToggle.hidden = true;
  trendsEl.hidden = true;
  interpretationEl.hidden = true;
  extractEl.hidden = true;
  modelsEl.hidden = true;
  documentationEl.hidden = true;
  pricesEl.hidden = true;
  dashboardEl.hidden = false;
  laneRow.hidden = false;
  dateFilterControl.hidden = false;
  refreshDashboardAll();
}

// Arrow-key roving focus for a segmented tablist.
function wireTablist(tabList, onSelect, keyOf) {
  tabList.forEach((tab, i) => {
    tab.addEventListener("click", () => onSelect(keyOf(tab)));
    tab.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      const dir = e.key === "ArrowRight" ? 1 : -1;
      const next = tabList[(i + dir + tabList.length) % tabList.length];
      next.focus();
      onSelect(keyOf(next));
    });
  });
}

function setupTabs() {
  // Lane tabs only; the page-nav (Board/Trends) keyboard + click handling lives in
  // the router, since those links drive navigation rather than scope a tablist.
  wireTablist(tabs, selectLane, (t) => t.dataset.lane);
}

function setupTheme() {
  const toggle = document.getElementById("theme-toggle");
  toggle.addEventListener("click", () => {
    const root = document.documentElement;
    const next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
    root.setAttribute("data-theme", next);
    localStorage.setItem("site-theme", next);
    // ECharts cannot re-theme a live instance, so rebuild the visible charts.
    if (router.current() === TRENDS_ROUTE) trends.rerenderFromCache();
    // The spend panel's donuts are ECharts too -- rebuild whichever page's panel is
    // showing (Interpretation and Prices each have their own instance).
    if (router.current() === INTERPRETATION_ROUTE) interpSpend.rerenderFromCache();
    if (router.current() === PRICES_ROUTE) pricesSpend.rerenderFromCache();
    if (router.current() === EXTRACT_ROUTE) extractSpend.rerenderFromCache();
    if (router.current() === DASHBOARD_ROUTE) dashboard.rerenderFromCache();
  });
}

// Toggle a video in/out of the tracked set (ephemeral; never written to the DB).
function setupTracking() {
  boardEl.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-track]");
    if (!btn || btn.disabled) return;
    const id = btn.dataset.track;
    if (state.tracked.has(id)) state.tracked.delete(id);
    else if (state.tracked.size < MAX_TRACKED) state.tracked.add(id);
    reloadBoard(); // re-render rows to reflect tracked / "5 max" state
    if (router.current() === TRENDS_ROUTE) trends.refreshTrajectory(state);
  });
}

// --- Phase 3 writes: star toggle + inline notes -----------------------------
// Delegated on boardEl (survives row re-renders). A star change requeries the
// board (keeps starred_only consistent); a note save updates in place with NO
// requery, so an in-progress edit elsewhere is never wiped.

function noteWrap(node) {
  return node.closest(".note-editor");
}

function setNoteError(wrap, message) {
  const span = wrap.querySelector(".note-error");
  if (span) span.textContent = message || "";
}

async function onStarClick(btn) {
  const id = btn.dataset.star;
  const next = btn.getAttribute("aria-pressed") !== "true";
  btn.disabled = true;
  try {
    await api.setStarred(id, next);
    await reloadBoard(); // the one requery case
  } catch (err) {
    btn.disabled = false;
    btn.title = `Could not save: ${err.message || err}`;
  }
}

async function onNoteSave(btn) {
  const wrap = noteWrap(btn);
  const ta = wrap.querySelector("[data-note]");
  const value = ta.value;
  btn.disabled = true;
  setNoteError(wrap, "");
  try {
    await api.setNotes(ta.dataset.note, value);
    ta.dataset.saved = value;
    wrap.classList.remove("dirty");
  } catch (err) {
    setNoteError(wrap, `Could not save: ${err.message || err}`);
  } finally {
    btn.disabled = false;
  }
}

function onNoteCancel(wrap) {
  const ta = wrap.querySelector("[data-note]");
  ta.value = ta.dataset.saved || "";
  wrap.classList.remove("dirty");
  setNoteError(wrap, "");
}

function setupWrites() {
  boardEl.addEventListener("click", (e) => {
    const star = e.target.closest("[data-star]");
    if (star && !star.disabled) return void onStarClick(star);
    const save = e.target.closest("[data-note-save]");
    if (save && !save.disabled) return void onNoteSave(save);
    const cancel = e.target.closest("[data-note-cancel]");
    if (cancel) return void onNoteCancel(noteWrap(cancel));
  });
  boardEl.addEventListener("input", (e) => {
    const ta = e.target.closest("[data-note]");
    if (!ta) return;
    const dirty = ta.value !== (ta.dataset.saved || "");
    noteWrap(ta).classList.toggle("dirty", dirty);
  });
  boardEl.addEventListener("keydown", (e) => {
    const ta = e.target.closest("[data-note]");
    if (!ta) return;
    if (e.key === "Escape") {
      onNoteCancel(noteWrap(ta));
    } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      const save = noteWrap(ta).querySelector("[data-note-save]");
      if (save) onNoteSave(save);
    }
  });
}

function setupDrawer() {
  drawerToggle.addEventListener("click", () => {
    const open = rail.classList.toggle("open");
    drawerToggle.setAttribute("aria-expanded", open ? "true" : "false");
  });
}

function setupFilters() {
  // Event delegation on the persistent container: any change/input re-runs the
  // board (debounced). The rail's children are replaced on lane/run change, so
  // delegation survives re-renders.
  railGroups.addEventListener("change", debouncedReload);
  railGroups.addEventListener("input", debouncedReload);
  document.getElementById("clear-filters").addEventListener("click", () => {
    railGroups.querySelectorAll("input").forEach((el) => {
      if (el.type === "checkbox") el.checked = false;
      else el.value = "";
    });
    reloadBoard();
  });
}

async function init() {
  setupTheme();
  setupTabs();
  setupDrawer();
  setupFilters();
  setupTracking();
  setupWrites();
  trends.init({
    bump: document.getElementById("chart-bump"),
    trajectory: document.getElementById("chart-trajectory"),
  });
  // interpretation.js mounts into the inner container (it replaceChildren's its
  // mount); spend.js owns the sibling panel, so a generation re-render never wipes
  // it. The outer #interpretation main stays the page region toggled by the router.
  interpretation.init(document.getElementById("interpretation-main"));
  // extract.js owns the inner #extract-main column (it replaceChildren's it); a spend.js
  // panel owns the sibling #extract-spend-panel aside, so a run re-render never wipes it
  // (the same two-stable-mounts pattern as Interpretation).
  extract.init(document.getElementById("extract-main"));
  // One spend-panel instance per page, each bound to its own aside mount.
  interpSpend = spend.create(document.getElementById(INTERP_SPEND_ID));
  pricesSpend = spend.create(document.getElementById(PRICES_SPEND_ID));
  extractSpend = spend.create(document.getElementById(EXTRACT_SPEND_ID));
  // Two interpretation-rail instances (factory), one per mount: the Trends rail and the
  // Dashboard rail (a sibling of the replaceChildren-owned #dashboard-main, so it is not
  // wiped on a sub-tab switch).
  trendsRail = railModule.create(document.getElementById("trends-interp-rail"));
  dashboardRail = railModule.create(document.getElementById("dashboard-interp-rail"));
  models.init(modelsEl);
  // price-refresh.js owns the Prices content column (#prices-main), NOT the #prices
  // shell, so its replaceChildren never wipes the sibling spend-panel aside.
  priceRefresh.init(document.getElementById(PRICES_MAIN_ID));
  documentation.init(documentationEl);
  // dashboard.js owns the INNER #dashboard-main column (it replaceChildren's it), NOT the
  // #dashboard shell, so its render never wipes the sibling interpretation rail aside.
  dashboard.init(document.getElementById("dashboard-main"));
  board.setActiveTab(app, tabs, state.lane);

  // A successful generation changes the spend totals; refresh the panel. The event
  // is dispatched by interpretation.js after a written run, keeping spend.js
  // decoupled (it never imports interpretation.js) while main.js owns `state`.
  document.addEventListener("interpretation:generated", () => interpSpend.refresh(state));

  // A completed extract run (success OR failure) may have spent classifier tokens, so
  // refresh the Extract spend panel. Unlike interpretation:generated (which only fires
  // while you are on the page), an extract run survives navigation and can finish while you
  // are off /extract, so gate on the route — refreshing a hidden panel would size ECharts on
  // a zero-size element. `extractSpend &&` guards this document-level listener against a
  // missing mount. The page-entry refresh catches up when you return.
  document.addEventListener("extract:completed", () => {
    if (extractSpend && router.current() === EXTRACT_ROUTE) extractSpend.refresh(state);
  });

  // Register routes (wires page-nav clicks/keys); do not dispatch until data loads.
  router.initRouter({
    routes: {
      "/board": enterBoard,
      "/trends": enterTrends,
      "/interpretation": enterInterpretation,
      "/dashboard": enterDashboard,
      "/extract": enterExtract,
      "/models": enterModels,
      "/prices": enterPrices,
      "/documentation": enterDocumentation,
    },
    // Leaving Documentation tears down any live native render (a pdf.js worker handle
    // outlives the hidden DOM otherwise); the page's own doc-to-doc switches are handled
    // inside documentation.js.
    leaves: {
      "/documentation": documentation.teardown,
    },
    links: pageNav,
    fallback: "/board",
  });

  // date-filter.js is the single source of truth. On a user change it dispatches
  // date-filter-change; sync state from the detail, then re-render ONLY the visible
  // page (guard on the current route) so background pages do no work. The control is
  // hidden on the non-date-scoped pages, so only board/trends/interpretation can be
  // the route here. The BOARD_ROUTE guard also prevents a double-fetch with the
  // enterBoard activation path: a filter change never navigates, so exactly one of
  // the two fires per action.
  document.addEventListener(EVENT_NAME, (e) => {
    const f = e.detail;
    state.runDate = f.active_run_date;
    state.startDate = f.start_date;
    state.endDate = f.end_date;
    const route = router.current();
    if (route === BOARD_ROUTE) onRunOrLaneChange();
    if (route === TRENDS_ROUTE) refreshTrendsCharts();
    if (route === INTERPRETATION_ROUTE) {
      interpretation.refresh(state);
      // The "This run" section is run-scoped, so an active-run change re-aggregates
      // it (month-to-date is unaffected but recomputes cheaply on the same call).
      interpSpend.refresh(state);
    }
    if (route === DASHBOARD_ROUTE) refreshDashboardAll();
  });

  let runs;
  try {
    runs = await api.getRuns();
  } catch (err) {
    board.renderError(boardEl, String(err.message || err));
    return;
  }
  if (!runs.length) {
    board.renderNoRuns(boardEl);
    dateFilterControl.hidden = true;
    drawerToggle.disabled = true;
    return;
  }
  // date-filter.js owns the filter. Initialize the control, then seed state from the
  // current (default "Latest run") filter. No pre-render here: router.start()
  // dispatches the landing route and its enterX handler renders from state via the
  // activation path (enterBoard reloads the board; enterTrends draws the charts).
  initDateFilter(runs);
  const f = getCurrentFilter();
  state.runDate = f.active_run_date;
  state.startDate = f.start_date;
  state.endDate = f.end_date;
  router.start();
}

init();
