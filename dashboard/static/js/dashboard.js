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
// How many top videos the breakouts bar chart shows. Kept small so each title-labeled
// bar fits the default .chart-mount height legibly; the leaderboard table lists the full set.
const TOP_BREAKOUTS = 10;

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
  if (section === "lifecycle") return api.getDashboardLifecycle(s.lane, s.startDate, s.endDate);
  return Promise.resolve(null);
}

async function renderActive() {
  if (!built || !lastState) return;
  const section = activeSection;
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
  else if (section === "lifecycle") renderLifecycle(data);
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
  const ratio = chartCard("Top breakouts (views-to-subs)");
  const lb = el("section", { class: "glass-panel chart-card dashboard-leaderboard" }, [
    el("h3", { class: "heading-sm", text: "Breakout leaderboard" }),
    leaderboardTable(data.leaderboard || []),
  ]);
  if (data.survivorship_note) {
    lb.appendChild(el("p", { class: "dashboard-caveat", text: SURVIVORSHIP_NOTE }));
  }
  panelEl.replaceChildren(
    el("div", { class: "dash-2col" }, [bands.card, ratio.card]), // counts | the ratio
    lb // full-width leaderboard below
  );
  charts.renderBars(bands.chart, data.band_counts, laneNow());
  charts.renderTopRatio(ratio.chart, (data.leaderboard || []).slice(0, TOP_BREAKOUTS), laneNow());
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

// The 3 lifecycle summary numbers as a compact KPI strip (mirrors titleStatsStrip).
// Null medians (empty lane+window) render as a dash, never "NaN" or "0".
function lifecycleKpiStrip(data) {
  const s = data.summary || {};
  const num = (v, digits) => (v == null ? "—" : Number(v).toFixed(digits));
  const cards = [
    ["Runs in window", String(data.run_count ?? 0)],
    ["Median runs ranked", num(s.median_runs_ranked, 0)],
    ["Median age (days)", num(s.median_days_since_publish, 1)],
  ];
  return el("section", { class: "dash-kpi-section" }, [
    el("h3", { class: "heading-sm dash-kpi-heading", text: "Lifecycle summary" }),
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

// A labeled lifecycle group: a heading-md title above its chart cards.
function lifecycleGroup(title, children) {
  return el("section", { class: "dashboard-lifecycle-group" }, [
    el("h2", { class: "heading-md", text: title }),
    ...children,
  ]);
}

function renderLifecycle(data) {
  if (!data) return;
  // Single-run guard: lifecycle charts need movement across runs. The default
  // "Latest run" window is one run, which would draw six degenerate/empty charts.
  if ((data.run_count || 0) < 2) {
    panelEl.replaceChildren(
      el("section", { class: "glass-panel chart-card dashboard-lifecycle" }, [
        el("h3", { class: "heading-sm", text: "Lifecycle" }),
        el("p", {
          class: "dashboard-status",
          text: "Select a wider period to see lifecycle trends across runs.",
        }),
      ])
    );
    return;
  }

  const lane = laneNow();
  const surv = data.survivorship || {};
  const eng = data.engagement || {};

  const growth = chartCard("Growth & velocity (fastest-growing)");
  const bump = chartCard("Rank movement (best-ranked)");
  const churn = chartCard("Roster churn");
  const appearances = chartCard("Appearances distribution");
  const ratio = chartCard("Like:comment ratio over time (fastest-growing)");
  const maturation = chartCard("Engagement vs age");

  panelEl.replaceChildren(
    lifecycleKpiStrip(data),
    lifecycleGroup("Growth", [growth.card]),
    lifecycleGroup("Survivorship", [
      el("div", { class: "dash-2col" }, [bump.card, churn.card]),
      appearances.card,
      el("p", { class: "dashboard-caveat", text: SURVIVORSHIP_NOTE }),
    ]),
    lifecycleGroup("Engagement", [
      el("div", { class: "dash-2col" }, [ratio.card, maturation.card]),
    ])
  );

  // Charts render after the mounts are in the DOM (ECharts needs real geometry).
  const hasGrowth = data.growth && data.growth.series && data.growth.series.length;
  if (hasGrowth) {
    charts.renderTrajectory(growth.chart, data.growth, lane);
  } else {
    // renderTrajectory's empty copy is Board-specific ("Track from the Board..."),
    // wrong here, so use a lifecycle-appropriate empty state instead.
    growth.chart.classList.add("chart-empty");
    growth.chart.textContent =
      "Growth curves appear once cohort videos have views across multiple runs.";
  }
  charts.renderBump(bump.chart, surv.rank_history || {}, lane);
  charts.renderChurn(churn.chart, surv.churn || [], lane);
  charts.renderBars(appearances.chart, surv.runs_ranked || [], lane);
  charts.renderRatioLines(ratio.chart, eng.ratio_series || [], lane);
  charts.renderMaturation(maturation.chart, eng.maturation || [], lane);
}

// Theme toggle: redraw only the active sub-tab's charts from cache (no refetch). Safe
// no-op for any sub-tab not yet fetched.
export function rerenderFromCache() {
  if (!built || !lastState) return;
  if (cache[activeSection]) renderSection(activeSection, cache[activeSection]);
}
