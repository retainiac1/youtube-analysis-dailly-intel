// The only module that touches the ECharts global (loaded via a vendored
// <script> in index.html). Presentation only; trends.js owns fetching + caching.
//
// ECharts cannot re-theme a live instance, so every render disposes any existing
// instance on the mount element and re-inits with the current theme. That makes
// the theme toggle a plain re-render (see trends.rerenderFromCache).

/* global echarts */

import { allocateSlots } from "./colors.js";

// Fixed lane-to-color map, identical to the rank badges / tabs (tokens.css).
export const LANE_COLORS = {
  health: "#00F2A9",
  habit: "#2D7CFF",
  overall: "#a855f7",
};

const RED = "#ef4444"; // negative signal only (a dropped/fell-off line)

// Rotating palette for multi-line charts (bump + trajectory). >= 20 visually
// distinct colours so the bump chart (up to TOP_N = 20 ranked videos) and the
// trajectory (<= 5 tracked) never collide among what is shown at once. RED is
// deliberately NOT in here: red is reserved for the fell-off / rank-drop signal.
const SERIES_PALETTE = [
  "#a855f7", "#2D7CFF", "#00F2A9", "#f59e0b", "#ec4899",
  "#22d3ee", "#eab308", "#14b8a6", "#8b5cf6", "#3b82f6",
  "#06b6d4", "#84cc16", "#f97316", "#d946ef", "#0ea5e9",
  "#10b981", "#6366f1", "#facc15", "#a3e635", "#38bdf8",
];

function paletteColor(slot) {
  return SERIES_PALETTE[slot % SERIES_PALETTE.length];
}

const intFmt = new Intl.NumberFormat();

function truncate(s, n) {
  const str = s == null ? "" : String(s);
  return str.length > n ? str.slice(0, n - 1) + "…" : str;
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}

const prefersReducedMotion =
  window.matchMedia &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Minimal theme objects. Backgrounds stay transparent so the glass panel shows
// through; only text/line/split colors flip with the document theme.
const THEMES = {
  dark: {
    backgroundColor: "transparent",
    textStyle: { color: "#b4bcd0" },
    axisLine: "#3a4256",
    splitLine: "rgba(255,255,255,0.06)",
  },
  light: {
    backgroundColor: "transparent",
    textStyle: { color: "#475569" },
    axisLine: "#cbd5e1",
    splitLine: "rgba(0,0,0,0.06)",
  },
};

function themeName() {
  return document.documentElement.getAttribute("data-theme") === "light"
    ? "light"
    : "dark";
}

// Live instances, so a window resize can re-flow them (an instance on a hidden
// element has zero size; trends.js only renders when the view is visible).
const instances = new Set();

window.addEventListener("resize", () => {
  for (const inst of instances) inst.resize();
});

function mount(el) {
  const existing = echarts.getInstanceByDom(el);
  if (existing) {
    instances.delete(existing);
    existing.dispose();
  }
  el.classList.remove("chart-empty");
  el.textContent = "";
  const inst = echarts.init(el);
  instances.add(inst);
  return inst;
}

function showEmpty(el, message) {
  const existing = echarts.getInstanceByDom(el);
  if (existing) {
    instances.delete(existing);
    existing.dispose();
  }
  el.classList.add("chart-empty");
  el.textContent = message;
}

function axes(t, opts) {
  return {
    xAxis: {
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color },
      ...opts.xAxis,
    },
    yAxis: {
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color },
      splitLine: { lineStyle: { color: t.splitLine } },
      ...opts.yAxis,
    },
  };
}

const baseGrid = { left: 48, right: 24, top: 28, bottom: 40 };

// --- Bump chart: rank movement across run_dates (rank 1 on top) --------------
export function renderBump(el, data, lane) {
  const runDates = data.run_dates || [];
  const series = data.series || [];
  if (!runDates.length || !series.length) {
    showEmpty(el, "No ranking history for this lane yet.");
    return;
  }
  // A bump chart needs at least two runs to show movement. The date filter can
  // window down to a single run (e.g. the default "Latest run"), so guide the user
  // to widen rather than draw a degenerate single-column chart.
  if (runDates.length < 2) {
    showEmpty(el, "Select a wider period to see rank movement.");
    return;
  }
  const t = THEMES[themeName()];
  const inst = mount(el);

  const lines = series.map((s, i) => {
    // Align points to the run_date axis; a missing run_date is a null (a gap),
    // which reads as the video having fallen off that run.
    const byDate = {};
    for (const p of s.points) byDate[p.run_date] = p.rank;
    const fellOff =
      s.points.length &&
      s.points[s.points.length - 1].run_date !== runDates[runDates.length - 1];
    // Explicit per-series colour (never the ECharts default palette, which
    // contains red): a distinct rotating colour, or RED only when fell off.
    const color = fellOff ? RED : paletteColor(i);
    return {
      name: s.title || s.video_id,
      type: "line",
      connectNulls: false,
      showSymbol: true,
      symbolSize: 7,
      data: runDates.map((d) => (d in byDate ? byDate[d] : null)),
      itemStyle: { color },
      lineStyle: fellOff ? { color, type: "dashed" } : { color },
    };
  });

  inst.setOption({
    animation: !prefersReducedMotion,
    // Per-item (not axis): hovering one line shows just that video's rank at the
    // hovered run. An axis tooltip listed every ranked video (~20 rows, taller than
    // the chart), which overflowed into the pinned header above and the next card
    // below. A single-item tooltip is small and stays clear of both.
    tooltip: {
      trigger: "item",
      formatter: (p) =>
        `<strong>${escapeHtml(truncate(p.seriesName, 50))}</strong><br/>` +
        `rank ${p.value} on ${escapeHtml(p.name)}`,
    },
    grid: baseGrid,
    ...axes(t, {
      xAxis: { type: "category", data: runDates, boundaryGap: false },
      yAxis: { type: "value", inverse: true, minInterval: 1, name: "rank" },
    }),
    series: lines,
  });
}

// --- Distribution histogram: view-count buckets (signature gradient) ---------
export function renderHistogram(el, data, lane) {
  const buckets = data.buckets || [];
  const t = THEMES[themeName()];
  const inst = mount(el);

  // Signature cyan-to-blue gradient on the bars, per the design standard, the
  // same regardless of lane.
  const gradient = new echarts.graphic.LinearGradient(0, 0, 0, 1, [
    { offset: 0, color: "#00F2A9" },
    { offset: 1, color: "#2D7CFF" },
  ]);

  // Tooltip lists the ranked videos behind the hovered bar: title (truncated) +
  // view count (un-truncated, right-aligned), already sorted desc by the API.
  function tooltipFor(dataIndex) {
    const b = buckets[dataIndex];
    if (!b) return "";
    const header = `<strong>${escapeHtml(b.label)}</strong> (${b.count})`;
    const list = b.videos || [];
    if (!list.length) return `${header}<br/>0 videos`;
    const rows = list
      .map(
        (v) =>
          `<div style="display:flex;justify-content:space-between;gap:12px;` +
          `max-width:320px">` +
          `<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">` +
          `${escapeHtml(truncate(v.title, 40))}</span>` +
          `<span style="text-align:right;white-space:nowrap">` +
          `${intFmt.format(v.view_count)}</span>` +
          `</div>`
      )
      .join("");
    return `${header}${rows}`;
  }

  inst.setOption({
    animation: !prefersReducedMotion,
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (params) => tooltipFor(params[0].dataIndex),
    },
    grid: baseGrid,
    ...axes(t, {
      xAxis: { type: "category", data: buckets.map((b) => b.label) },
      yAxis: { type: "value", minInterval: 1, name: "videos" },
    }),
    series: [
      {
        type: "bar",
        data: buckets.map((b) => b.count),
        itemStyle: { color: gradient, borderRadius: [4, 4, 0, 0] },
      },
    ],
  });
}

// --- Trajectory: per-video view count + velocity over time -------------------
// Persistent video_id -> palette slot, so a tracked video keeps its colour when
// another video is tracked/untracked (colour is keyed to identity, not position).
let trajectorySlots = {};

export function renderTrajectory(el, payload, lane) {
  const series = (payload && payload.series) || [];
  if (series.length === 0) {
    showEmpty(
      el,
      "Track videos from the Board to see their view trajectory over time."
    );
    return;
  }
  // Tracked, but nothing has two points yet: a single snapshot cannot draw a line.
  const hasLine = series.some((s) => s.points && s.points.length >= 2);
  if (!hasLine) {
    showEmpty(
      el,
      "Collecting data, trajectories appear once a video has views across multiple runs."
    );
    return;
  }

  const withData = series.filter((s) => s.points && s.points.length);
  const t = THEMES[themeName()];
  const inst = mount(el);

  // Stable, collision-free colour per video (legend, line, points, and tooltip
  // marker all derive from the series colour, so they cannot disagree).
  trajectorySlots = allocateSlots(withData.map((s) => s.video_id), trajectorySlots);

  const lines = [];
  withData.forEach((s) => {
    const color = paletteColor(trajectorySlots[s.video_id]);
    const label = s.title || s.video_id; // titles are required upstream
    lines.push({
      name: `${label} views`,
      type: "line",
      showSymbol: true,
      symbolSize: 6,
      itemStyle: { color },
      lineStyle: { color },
      data: s.points.map((p) => [p.captured_at, p.view_count]),
    });
    if (s.velocity && s.velocity.length) {
      lines.push({
        name: `${label} views/day`,
        type: "line",
        yAxisIndex: 1,
        showSymbol: true,
        symbolSize: 5,
        itemStyle: { color },
        lineStyle: { color, type: "dashed" },
        data: s.velocity.map((v) => [v.captured_at, v.views_per_day]),
      });
    }
  });

  inst.setOption({
    animation: !prefersReducedMotion,
    tooltip: { trigger: "axis" },
    legend: {
      textStyle: { color: t.textStyle.color },
      top: 0,
      type: "scroll",
      // Full title is the series name (so the axis tooltip shows it in full);
      // truncate only the legend label.
      formatter: (name) => truncate(name, 40),
    },
    grid: { ...baseGrid, right: 56, top: 36 },
    xAxis: {
      type: "time",
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color },
    },
    yAxis: [
      {
        type: "value",
        name: "views",
        axisLine: { lineStyle: { color: t.axisLine } },
        axisLabel: { color: t.textStyle.color },
        splitLine: { lineStyle: { color: t.splitLine } },
      },
      {
        type: "value",
        name: "views/day",
        axisLine: { lineStyle: { color: t.axisLine } },
        axisLabel: { color: t.textStyle.color },
        splitLine: { show: false },
      },
    ],
    series: lines,
  });
}
