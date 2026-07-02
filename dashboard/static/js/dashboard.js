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

// Selectable line counts for the Lifecycle "Views per day over time" curve (it
// overplots at the full growth cohort). Coupled set, defined once; options are
// filtered to those that fit the actually-available series. velocityLimit is
// ephemeral view state (survives refetch/theme re-render), not persisted.
const VELOCITY_LINE_STEPS = [5, 8, 12, 20];
let velocityLimit = 8;

const intFmt = new Intl.NumberFormat();

let mountEl = null;
let panelEl = null;
// The period-aware view-count distribution: a persistent card ABOVE the sub-tab nav (a
// lane+window overview that belongs to no single sub-tab). distEl is its chart mount,
// built once and never inside panelEl, so a sub-tab swap (which replaceChildren's
// panelEl) never wipes it. distCache holds the last payload for a theme redraw.
let distEl = null;
let distCache = null;
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
  distEl = el("div", { class: "chart-mount" });
  const distCard = el(
    "section",
    { class: "glass-panel chart-card dashboard-distribution" },
    [
      el("h3", { class: "heading-sm", text: "View-count distribution" }),
      el("p", {
        class: "dashboard-caption",
        text: "Distinct lane videos over the selected period, by latest view count.",
      }),
      distEl,
    ]
  );
  // Overview card first, then the tabbed detail. distCard + nav are stable siblings;
  // only panelEl is swapped on a sub-tab change.
  mountEl.replaceChildren(distCard, buildNav(), panelEl);
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
  // The persistent distribution card and the active sub-tab both refetch for the new
  // lane/window; they are independent, so run them concurrently.
  await Promise.all([renderActive(), refreshDistribution(lastState)]);
}

// Fetch + draw the period-aware view-count distribution for the given lane/window. Uses
// the same lastState-identity supersede guard as renderActive: a lane/window change
// mid-flight reassigns lastState, so a stale result is dropped.
async function refreshDistribution(s) {
  let data;
  try {
    data = await api.getDistribution(s.lane, s.startDate, s.endDate);
  } catch (err) {
    if (s === lastState) {
      distCache = null;
      distEl.replaceChildren(
        el("p", { class: "dashboard-status error", text: String(err.message || err) })
      );
    }
    return;
  }
  if (s !== lastState) return;
  distCache = data;
  charts.renderHistogram(distEl, data, s.lane);
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
// These describe how lane videos behave over time (from stats_snapshots), not board
// trivia. Null medians (empty lane+window) render as "n/a", never "NaN", "0", or an
// em dash.
function lifecycleKpiStrip(data) {
  const s = data.summary || {};
  const naFixed = (v, digits) => (v == null ? "n/a" : Number(v).toFixed(digits));
  const naInt = (v) => (v == null ? "n/a" : Math.round(Number(v)).toLocaleString());
  const cards = [
    ["Median tracked lifespan (days)", naFixed(s.median_tracked_lifespan_days, 1)],
    ["Median peak velocity (views/day)", naInt(s.median_peak_velocity)],
    ["Median total view growth (views)", naInt(s.median_total_view_growth)],
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

// A chart card whose title is WHAT the chart shows, with the cohort/scope as a
// caption underneath (the reader can't see the selection rule, so it never goes
// in the title).
function lcCard(title, caption, control) {
  const chart = el("div", { class: "chart-mount" });
  const heading = control
    ? el("div", { class: "lc-card-head" }, [
        el("h3", { class: "heading-sm", text: title }),
        control,
      ])
    : el("h3", { class: "heading-sm", text: title });
  const kids = [heading];
  if (caption) kids.push(el("p", { class: "dashboard-caption", text: caption }));
  kids.push(chart);
  return { card: el("section", { class: "glass-panel chart-card" }, kids), chart };
}

function renderLifecycle(data) {
  if (!data) return;
  // No tab-level single-run blank: the per-video curves are snapshot-driven, so a
  // video with several snapshots has a real curve even in a single board run. Only
  // Rank movement (a board metric) needs more than one run; it guards itself below.
  const lane = laneNow();

  const periodNote =
    "The period selects which videos belong to the lane; each curve shows that " +
    "video's full tracked life (every snapshot, not just on-board runs).";

  const cohortNote =
    "The top 20 videos by peak growth (highest views per day reached at any point), " +
    "over each video's full tracked life from its first capture.";
  const viewGrowth = lcCard(
    "View growth",
    `Cumulative view count. ${cohortNote} ${periodNote}`
  );
  // The Views-per-day curve overplots at the full cohort, so the line count is a
  // user control (ephemeral view state). Options adapt to how many series exist;
  // velocityLimit is the preference, velEffective the clamped value actually shown.
  const velAvail = (data.growth?.series || []).filter(
    (s) => s.velocity && s.velocity.length
  ).length;
  const velOpts = (() => {
    const steps = VELOCITY_LINE_STEPS.filter((n) => n < velAvail);
    if (velAvail > 0 && !steps.includes(velAvail)) steps.push(velAvail);
    return [...new Set(steps)].sort((a, b) => a - b);
  })();
  const velEffective =
    velOpts.filter((n) => n <= velocityLimit).pop() ??
    velOpts[velOpts.length - 1] ??
    0;
  const velSelect =
    velOpts.length > 1
      ? el("select", {
          class: "lc-line-select",
          "aria-label": "Number of velocity lines to show",
        })
      : null;
  if (velSelect) {
    velOpts.forEach((n) =>
      velSelect.appendChild(
        el("option", {
          value: String(n),
          text: n === velAvail ? `all (${n})` : String(n),
        })
      )
    );
    velSelect.value = String(velEffective);
  }
  const velControl = velSelect
    ? el("label", { class: "lc-line-control" }, [
        el("span", { class: "lc-line-label", text: "Show top" }),
        velSelect,
      ])
    : null;
  const velocityLines = lcCard(
    "Views per day over time",
    "Views per day over each video's full tracked life, same cohort as View growth. " +
      "The rise to peak and the decline after are both visible (cumulative view " +
      "growth can only flatten, never fall).",
    velControl
  );
  if (velSelect) {
    velSelect.addEventListener("change", () => {
      velocityLimit = Number(velSelect.value);
      charts.renderVelocityLines(velocityLines.chart, data.growth || {}, lane, velocityLimit);
    });
  }
  const velocity = lcCard(
    "Peak velocity",
    "Each cohort video's peak views per day, the high point of its curve to the left, ranked."
  );
  const bump = lcCard(
    "Rank movement",
    "Rank recomputed each day: each video's views-to-subscriber ratio against the " +
      "other lane videos that day, over its full tracked life (rank 1 = best). " +
      "Subscriber history is captured from the migration date onward, so earlier " +
      "days use current subscriber counts."
  );
  const lifespan = lcCard(
    "Tracked-lifespan distribution",
    "How many days each lane video has been tracked, first snapshot to last. " +
      "Videos retire about 30 days after their last view growth."
  );
  const ratio = lcCard("Likes per comment over time", `Engagement mix over each video's tracked life. ${periodNote}`);
  const maturation = lcCard(
    "Engagement vs age",
    "Every in-window video: likes per comment (log) vs days since published, colored by view count."
  );

  panelEl.replaceChildren(
    lifecycleKpiStrip(data),
    lifecycleGroup("Growth", [
      el("div", { class: "dash-2col" }, [viewGrowth.card, velocityLines.card]),
      velocity.card,
    ]),
    lifecycleGroup("Survivorship", [
      el("div", { class: "dash-2col" }, [bump.card, lifespan.card]),
    ]),
    lifecycleGroup("Engagement", [
      el("div", { class: "dash-2col" }, [ratio.card, maturation.card]),
    ])
  );

  // Charts render after the mounts are in the DOM (ECharts needs real geometry).
  charts.renderGrowthLines(viewGrowth.chart, data.growth || {}, lane);
  charts.renderVelocityLines(velocityLines.chart, data.growth || {}, lane, velEffective);
  charts.renderVelocityBars(velocity.chart, data.growth || {}, lane);
  // Rank movement self-guards: renderRankMovement needs a series with >= 2 ranked
  // DAYS (not >= 2 board runs), so a single board run still draws when the window
  // spans multiple snapshot days. No tab-level run_count gate.
  charts.renderRankMovement(bump.chart, data.rank_history || {}, lane);
  charts.renderBars(lifespan.chart, data.lifespan_distribution || [], lane);
  charts.renderRatioLines(ratio.chart, data.ratio_series || [], lane);
  charts.renderMaturation(maturation.chart, data.maturation || [], lane);
}

// Theme toggle: redraw only the active sub-tab's charts from cache (no refetch). Safe
// no-op for any sub-tab not yet fetched.
export function rerenderFromCache() {
  if (!built || !lastState) return;
  if (cache[activeSection]) renderSection(activeSection, cache[activeSection]);
  if (distCache) charts.renderHistogram(distEl, distCache, laneNow());
}
