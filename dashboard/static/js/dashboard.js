// The Dashboard page: a lane-scoped, period-aggregated view across four sub-tabs
// (Topic mix, Breakout, Format & craft, Lifecycle). State + fetching + caching live
// here; the ECharts rendering reuses charts.js. The sub-tab nav is rendered ONCE and
// only #dashboard-section-panel is swapped on changes, so the nav handlers survive.

import * as api from "./api.js";
import * as charts from "./charts.js";

const SECTIONS = [
  { key: "topic-mix", label: "Topic mix" },
  { key: "breakout", label: "Breakout" },
  { key: "format", label: "Format & craft" },
  { key: "lifecycle", label: "Lifecycle" },
];
const DEFAULT_SECTION = "topic-mix";
const SURVIVORSHIP_NOTE =
  "Best among caught, not best on YouTube. Conditioned on clearing the capture view bar.";

const intFmt = new Intl.NumberFormat();

let mountEl = null;
let panelEl = null;
let activeSection = DEFAULT_SECTION;
let built = false;
// The lane + window the panel was last rendered for. Reassigned (new object) on every
// refresh, so an in-flight fetch can detect it was superseded by comparing identity.
let lastState = null;
const cache = {}; // section key -> last payload fetched for lastState

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (v != null) node.setAttribute(k, v);
  }
  for (const c of children) if (c) node.appendChild(c);
  return node;
}

export function init(mount) {
  mountEl = mount;
}

function buildNav() {
  const nav = el("nav", {
    class: "lane-tabs dashboard-subtabs",
    role: "tablist",
    "aria-label": "Dashboard section",
  });
  for (const s of SECTIONS) {
    const active = s.key === activeSection;
    nav.appendChild(
      el("button", {
        class: "lane-tab dashboard-subtab",
        type: "button",
        role: "tab",
        "data-section": s.key,
        "aria-selected": active ? "true" : "false",
        tabindex: active ? "0" : "-1",
        text: s.label,
      })
    );
  }
  const subtabs = [...nav.querySelectorAll(".dashboard-subtab")];
  subtabs.forEach((tab, i) => {
    tab.addEventListener("click", () => selectSection(tab.dataset.section, subtabs));
    tab.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      const dir = e.key === "ArrowRight" ? 1 : -1;
      const next = subtabs[(i + dir + subtabs.length) % subtabs.length];
      next.focus();
      selectSection(next.dataset.section, subtabs);
    });
  });
  return nav;
}

function ensureBuilt() {
  if (built) return;
  panelEl = el("div", { id: "dashboard-section-panel" });
  mountEl.replaceChildren(buildNav(), panelEl);
  built = true;
}

function selectSection(key, subtabs) {
  if (key === activeSection) return;
  activeSection = key;
  for (const tab of subtabs) {
    const on = tab.dataset.section === key;
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
  }
  renderActive();
}

// Called on page entry, date-filter change, and lane change. The lane/window may have
// changed, so cached section payloads are stale: clear them and re-render the active tab.
export async function refresh(state) {
  ensureBuilt();
  lastState = { lane: state.lane, startDate: state.startDate, endDate: state.endDate };
  for (const k of Object.keys(cache)) delete cache[k];
  await renderActive();
}

function fetchSection(section, s) {
  if (section === "topic-mix") return api.getDashboardTopicMix(s.lane, s.startDate, s.endDate);
  if (section === "breakout") return api.getDashboardBreakout(s.lane, s.startDate, s.endDate);
  if (section === "format") return api.getDashboardFormat(s.lane, s.startDate, s.endDate);
  return Promise.resolve(null);
}

async function renderActive() {
  if (!built || !lastState) return;
  const section = activeSection;
  if (section === "lifecycle") {
    renderLifecycle();
    return;
  }
  const s = lastState;
  if (!cache[section]) {
    panelEl.replaceChildren(el("p", { class: "dashboard-status", text: "Loading..." }));
    let data;
    try {
      data = await fetchSection(section, s);
    } catch (err) {
      if (section === activeSection && s === lastState) {
        panelEl.replaceChildren(
          el("p", { class: "dashboard-status error", text: String(err.message || err) })
        );
      }
      return;
    }
    // Supersede guard: the user may have switched sub-tab, or the lane/window may have
    // changed (new lastState), while this fetch was in flight. If so, drop this result.
    if (section !== activeSection || s !== lastState) return;
    cache[section] = data;
  }
  renderSection(section, cache[section]);
}

function laneNow() {
  return lastState ? lastState.lane : "health";
}

function chartCard(title) {
  const chart = el("div", { class: "chart-mount" });
  const card = el("section", { class: "glass-panel chart-card" }, [
    el("h3", { class: "heading-sm", text: title }),
    chart,
  ]);
  return { card, chart };
}

function renderSection(section, data) {
  if (section === "topic-mix") renderTopicMix(data);
  else if (section === "breakout") renderBreakout(data);
  else if (section === "format") renderFormat(data);
}

function renderTopicMix(data) {
  const sub = chartCard("Sub-niche volume");
  const cat = chartCard("YouTube category");
  const top = chartCard("Topic tags");
  panelEl.replaceChildren(
    el("div", { class: "dash-2col" }, [sub.card, cat.card]),
    top.card // full width (flex-column panel child stretches)
  );
  charts.renderBars(sub.chart, data.subniche_counts, laneNow());
  charts.renderBars(cat.chart, data.category_counts, laneNow());
  charts.renderBars(top.chart, data.topic_counts, laneNow());
}

function renderBreakout(data) {
  const bands = chartCard("Views-to-subs ratio bands");
  const lb = el("section", { class: "glass-panel chart-card dashboard-leaderboard" }, [
    el("h3", { class: "heading-sm", text: "Breakout leaderboard" }),
    leaderboardTable(data.leaderboard || []),
  ]);
  if (data.survivorship_note) {
    lb.appendChild(el("p", { class: "dashboard-caveat", text: SURVIVORSHIP_NOTE }));
  }
  bands.card.classList.add("dash-bands"); // short 4-bar chart: cap width, do not full-bleed
  panelEl.replaceChildren(bands.card, lb); // bands (capped) then full-width leaderboard
  charts.renderBars(bands.chart, data.band_counts, laneNow());
}

function leaderboardTable(rows) {
  if (!rows.length) {
    return el("p", { class: "dashboard-status", text: "No breakouts in this window." });
  }
  const wrap = el("div", { class: "board dashboard-lb" });
  rows.forEach((r, i) => {
    wrap.appendChild(
      el("div", { class: "row dashboard-lb-row" }, [
        el("span", { class: "dashboard-lb-rank", text: `#${i + 1}` }),
        el("span", { class: "dashboard-lb-title", title: r.title || "", text: r.title || r.video_id }),
        el("span", { class: "dashboard-lb-channel", text: r.channel_title || "" }),
        el("span", { class: "dashboard-lb-views", text: intFmt.format(r.view_count ?? 0) }),
        el("span", { class: "dashboard-lb-ratio", text: `${Number(r.views_to_subs_ratio).toFixed(1)}x` }),
        el("span", { class: "dashboard-lb-subs", text: intFmt.format(r.subscriber_count ?? 0) }),
      ])
    );
  });
  return wrap;
}

function renderFormat(data) {
  const dur = chartCard("Duration distribution");
  const scatter = chartCard("Likes vs comments");
  const heat = chartCard("Publish times (Eastern)");
  panelEl.replaceChildren(
    titleStatsStrip(data.title_stats || {}), // KPI strip on top (full width)
    el("div", { class: "dash-2col" }, [dur.card, scatter.card]), // paired, equal height
    heat.card // wide chart full width
  );
  charts.renderDurationHistogram(dur.chart, data.durations || [], laneNow());
  charts.renderScatter(scatter.chart, data.like_comment_pairs || [], laneNow());
  charts.renderHeatmap(heat.chart, data.publish_heatmap || [], laneNow());
}

// The 4 title-anatomy numbers as a compact, labeled KPI strip (no big bordered card).
function titleStatsStrip(s) {
  const pct = (v) => `${Number(v ?? 0).toFixed(1)}%`;
  const cards = [
    ["Avg length", `${Number(s.avg_length ?? 0).toFixed(0)}`],
    ["Has a number", pct(s.pct_has_number)],
    ["Has a question", pct(s.pct_has_question)],
    ["Has an emoji", pct(s.pct_has_emoji)],
  ];
  return el("section", { class: "dash-kpi-section" }, [
    el("h3", { class: "heading-sm dash-kpi-heading", text: "Title anatomy" }),
    el(
      "div",
      { class: "dash-kpis" },
      cards.map(([label, value]) =>
        el("div", { class: "stat dashboard-statcard" }, [
          el("span", { class: "stat-value", text: value }),
          el("span", { class: "stat-label", text: label }),
        ])
      )
    ),
  ]);
}

function renderLifecycle() {
  panelEl.replaceChildren(
    el("section", { class: "glass-panel chart-card dashboard-lifecycle" }, [
      el("h3", { class: "heading-sm", text: "Lifecycle" }),
      el("p", {
        class: "dashboard-status",
        text:
          "These charts require daily snapshot history from the pipeline. They will " +
          "populate as runs accumulate.",
      }),
    ])
  );
}

// Theme toggle: redraw only the active sub-tab's charts from cache (no refetch). Safe
// no-op for Lifecycle (no chart) and for any sub-tab not yet fetched.
export function rerenderFromCache() {
  if (!built || !lastState) return;
  if (activeSection === "lifecycle") return;
  if (cache[activeSection]) renderSection(activeSection, cache[activeSection]);
}
