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
import * as interpRail from "./interpretation-rail.js";
import * as spend from "./spend.js";
import * as models from "./models.js";
import * as priceRefresh from "./price-refresh.js";
import * as documentation from "./documentation.js";
import * as router from "./router.js";
import "./sticky-header.js"; // side-effect: publishes --header-h for the pinned strip

const app = document.getElementById("app");
const runSelect = document.getElementById("run-select");
const boardEl = document.getElementById("board");
const trendsEl = document.getElementById("trends");
const interpretationEl = document.getElementById("interpretation");
const modelsEl = document.getElementById("models");
const pricesEl = document.getElementById("prices");
const documentationEl = document.getElementById("documentation");
const railGroups = document.getElementById("filter-groups");
const rail = document.getElementById("filter-rail");
const drawerToggle = document.getElementById("filter-drawer-toggle");
const laneRow = document.querySelector(".lane-row");
const tabs = [...document.querySelectorAll(".lane-tab")];
const pageNav = [...document.querySelectorAll(".page-nav-link")];

const MAX_TRACKED = 5;
const TRENDS_ROUTE = "/trends";
const INTERPRETATION_ROUTE = "/interpretation";

// The active page is owned by the router (router.current()); run/lane/tracked are
// shared state that persists across pages. tracked is EPHEMERAL in-memory view
// state (which videos are charted), distinct from starred (a Phase 3 DB write).
const state = { runDate: null, lane: "health", tracked: new Set() };

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

// A run change refreshes the histogram (the one run-scoped chart) and the
// interpretation rail (the summary is keyed on run + lane); the bump chart and
// trajectory span all runs and only change with the lane.
async function onRunChangeTrends() {
  await trends.refreshHistogram(state);
  interpRail.refresh(state);
}

async function onLaneChangeTrends() {
  await trends.refreshAll(state);
  interpRail.refresh(state);
}

function selectLane(lane) {
  if (lane === state.lane) return;
  state.lane = lane;
  board.setActiveTab(app, tabs, lane);
  onRunOrLaneChange();
  if (router.current() === TRENDS_ROUTE) onLaneChangeTrends();
  if (router.current() === INTERPRETATION_ROUTE) interpretation.refresh(state);
}

// Route handlers: show the page's content region and hide the others. These only
// toggle visibility (they do not re-render the board), so Board's rows and filter
// state survive navigating away to another page and back. The board's data is
// loaded by run/lane changes and the initial load, independent of which page shows.
function enterBoard() {
  boardEl.hidden = false;
  rail.hidden = false;
  drawerToggle.hidden = false;
  trendsEl.hidden = true;
  interpretationEl.hidden = true;
  modelsEl.hidden = true;
  documentationEl.hidden = true;
  pricesEl.hidden = true;
  laneRow.hidden = false;
}

function enterTrends() {
  boardEl.hidden = true;
  rail.hidden = true;
  drawerToggle.hidden = true;
  trendsEl.hidden = false;
  interpretationEl.hidden = true;
  modelsEl.hidden = true;
  documentationEl.hidden = true;
  pricesEl.hidden = true;
  laneRow.hidden = false;
  // Render the charts on entry: an ECharts instance on a hidden element cannot
  // size itself, so charts are only rendered while Trends is visible.
  trends.refreshAll(state);
  // The read-only interpretation rail beside the charts (run + lane scoped).
  interpRail.refresh(state);
}

function enterInterpretation() {
  boardEl.hidden = true;
  rail.hidden = true;
  drawerToggle.hidden = true;
  trendsEl.hidden = true;
  interpretationEl.hidden = false;
  modelsEl.hidden = true;
  documentationEl.hidden = true;
  pricesEl.hidden = true;
  laneRow.hidden = false;
  // Fetch + render the interpretation for the current run + lane on entry.
  interpretation.refresh(state);
  // The spend panel beside it (run section + month-to-date), refreshed on entry.
  spend.refresh(state);
}

// The registry editor: a global admin page (not run/lane scoped). Hide every other
// region + the filter rail/drawer, then load the models grid.
function enterModels() {
  boardEl.hidden = true;
  rail.hidden = true;
  drawerToggle.hidden = true;
  trendsEl.hidden = true;
  interpretationEl.hidden = true;
  modelsEl.hidden = false;
  documentationEl.hidden = true;
  pricesEl.hidden = true;
  laneRow.hidden = false;
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
  modelsEl.hidden = true;
  pricesEl.hidden = true;
  documentationEl.hidden = false;
  laneRow.hidden = true; // the page is not lane-scoped; hide the lane tabs here
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
  modelsEl.hidden = true;
  documentationEl.hidden = true;
  pricesEl.hidden = false;
  laneRow.hidden = true; // not lane-scoped; hide the lane tabs here
  priceRefresh.refresh();
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
    // The spend panel's donuts are ECharts too -- rebuild them on the same toggle.
    if (router.current() === INTERPRETATION_ROUTE) spend.rerenderFromCache();
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
    distribution: document.getElementById("chart-distribution"),
    trajectory: document.getElementById("chart-trajectory"),
  });
  // interpretation.js mounts into the inner container (it replaceChildren's its
  // mount); spend.js owns the sibling panel, so a generation re-render never wipes
  // it. The outer #interpretation main stays the page region toggled by the router.
  interpretation.init(document.getElementById("interpretation-main"));
  spend.init(document.getElementById("spend-panel"));
  interpRail.init(document.getElementById("trends-interp-rail"));
  models.init(modelsEl);
  priceRefresh.init(pricesEl);
  documentation.init(documentationEl);
  board.setActiveTab(app, tabs, state.lane);

  // A successful generation changes the spend totals; refresh the panel. The event
  // is dispatched by interpretation.js after a written run, keeping spend.js
  // decoupled (it never imports interpretation.js) while main.js owns `state`.
  document.addEventListener("interpretation:generated", () => spend.refresh(state));

  // Register routes (wires page-nav clicks/keys); do not dispatch until data loads.
  router.initRouter({
    routes: {
      "/board": enterBoard,
      "/trends": enterTrends,
      "/interpretation": enterInterpretation,
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

  runSelect.addEventListener("change", () => {
    state.runDate = runSelect.value;
    onRunOrLaneChange();
    if (router.current() === TRENDS_ROUTE) onRunChangeTrends();
    if (router.current() === INTERPRETATION_ROUTE) {
      interpretation.refresh(state);
      // The "This run" section is run-scoped, so a run change re-aggregates it
      // (month-to-date is unaffected but recomputes cheaply on the same call).
      spend.refresh(state);
    }
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
    runSelect.disabled = true;
    drawerToggle.disabled = true;
    return;
  }
  board.populateRunSelect(runSelect, runs);
  state.runDate = runs[0];
  // Load the board (rows + filter options) so it is ready regardless of the
  // landing page, then dispatch the current URL: "/" redirects to /board, and a
  // deep link to /trends renders the charts via enterTrends.
  await onRunOrLaneChange();
  router.start();
}

init();
