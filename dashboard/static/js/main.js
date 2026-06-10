// Bootstrap: load runs + lane options, render the board, and wire the run
// picker, page nav (router), lane tabs, filter rail (debounced), theme toggle,
// and mobile drawer. Board and Trends are URL-routed pages (/board, /trends); the
// router swaps which page is shown, while the run and lane persist across both.

import * as api from "./api.js";
import * as filters from "./filters.js";
import * as board from "./board.js";
import * as trends from "./trends.js";
import * as router from "./router.js";
import "./sticky-header.js"; // side-effect: publishes --header-h for the pinned strip

const app = document.getElementById("app");
const runSelect = document.getElementById("run-select");
const boardEl = document.getElementById("board");
const trendsEl = document.getElementById("trends");
const railGroups = document.getElementById("filter-groups");
const rail = document.getElementById("filter-rail");
const drawerToggle = document.getElementById("filter-drawer-toggle");
const tabs = [...document.querySelectorAll(".lane-tab")];
const pageNav = [...document.querySelectorAll(".page-nav-link")];

const MAX_TRACKED = 5;
const TRENDS_ROUTE = "/trends";

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

// A run change refreshes only the histogram (the one run-scoped chart); the bump
// chart and trajectory span all runs and only change with the lane.
async function onRunChangeTrends() {
  await trends.refreshHistogram(state);
}

async function onLaneChangeTrends() {
  await trends.refreshAll(state);
}

function selectLane(lane) {
  if (lane === state.lane) return;
  state.lane = lane;
  board.setActiveTab(app, tabs, lane);
  onRunOrLaneChange();
  if (router.current() === TRENDS_ROUTE) onLaneChangeTrends();
}

// Route handlers: show the page's content region and hide the others. These only
// toggle visibility (they do not re-render the board), so Board's rows and filter
// state survive navigating away to Trends and back. The board's data is loaded by
// run/lane changes and the initial load, independent of which page is shown.
function enterBoard() {
  boardEl.hidden = false;
  rail.hidden = false;
  drawerToggle.hidden = false;
  trendsEl.hidden = true;
}

function enterTrends() {
  boardEl.hidden = true;
  rail.hidden = true;
  drawerToggle.hidden = true;
  trendsEl.hidden = false;
  // Render the charts on entry: an ECharts instance on a hidden element cannot
  // size itself, so charts are only rendered while Trends is visible.
  trends.refreshAll(state);
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
  trends.init({
    bump: document.getElementById("chart-bump"),
    distribution: document.getElementById("chart-distribution"),
    trajectory: document.getElementById("chart-trajectory"),
  });
  board.setActiveTab(app, tabs, state.lane);

  // Register routes (wires page-nav clicks/keys); do not dispatch until data loads.
  router.initRouter({
    routes: { "/board": enterBoard, "/trends": enterTrends },
    links: pageNav,
    fallback: "/board",
  });

  runSelect.addEventListener("change", () => {
    state.runDate = runSelect.value;
    onRunOrLaneChange();
    if (router.current() === TRENDS_ROUTE) onRunChangeTrends();
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
