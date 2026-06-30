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
// Compact axis labels (1.2M, 600K) so a wide value range never clips the grid edge.
const compactFmt = new Intl.NumberFormat(undefined, {
  notation: "compact",
  maximumFractionDigits: 1,
});

function truncate(s, n) {
  const str = s == null ? "" : String(s);
  // Code-point aware: slicing a JS string by .length can cut a multi-byte emoji
  // in half and render a broken char. Array.from splits on code points, so an
  // emoji survives or is dropped whole.
  const cps = Array.from(str);
  return cps.length > n ? cps.slice(0, n - 1).join("") + "…" : str;
}

// Short date tick for time axes ("Jun 19"). ECharts passes a ms timestamp; format
// in the browser's local zone (the dashboard is an Eastern-local tool).
const TICK_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
  "Sep", "Oct", "Nov", "Dec"];
function fmtDateTick(ms) {
  const d = new Date(ms);
  return `${TICK_MONTHS[d.getMonth()]} ${d.getDate()}`;
}

// Short label for a "YYYY-MM-DD" run-date key -> "Jun 8". Parsed by field (NOT
// new Date(str), which would shift the day across a timezone boundary).
function fmtRunDate(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || "");
  return m ? `${TICK_MONTHS[+m[2] - 1]} ${+m[3]}` : String(s == null ? "" : s);
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}

// A video title rendered as a clickable YouTube link for use inside ECharts HTML
// tooltips. Canvas legends and on-line labels cannot hold real anchors, so the
// tooltip is where a named video becomes clickable. href is the pre-built
// videos.link; with no link it falls back to plain escaped text. Both title and
// href are escaped (the title can carry arbitrary user-facing text).
function videoTitleLink(title, link) {
  const label = escapeHtml(truncate(title || "(untitled)", 60));
  if (!link) return label;
  return (
    `<a href="${escapeHtml(link)}" target="_blank" rel="noopener" ` +
    `style="color:inherit;text-decoration:underline">${label}</a>`
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
    // top:48 clears the scroll legend; containLabel + compact labels keep the
    // (possibly 6-digit) value labels inside the grid. Axis names sit vertically
    // mid-axis (nameLocation:"middle") so they never collide with the top legend.
    grid: { left: 64, right: 64, top: 48, bottom: 40 },
    xAxis: {
      type: "time",
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color },
    },
    yAxis: [
      {
        type: "value",
        name: "views",
        nameLocation: "middle",
        nameGap: 48,
        nameTextStyle: { color: t.textStyle.color },
        axisLine: { lineStyle: { color: t.axisLine } },
        axisLabel: { color: t.textStyle.color, formatter: (v) => compactFmt.format(v) },
        splitLine: { lineStyle: { color: t.splitLine } },
      },
      {
        type: "value",
        name: "views/day",
        nameLocation: "middle",
        nameGap: 48,
        nameRotate: -90,
        nameTextStyle: { color: t.textStyle.color },
        axisLine: { lineStyle: { color: t.axisLine } },
        axisLabel: { color: t.textStyle.color, formatter: (v) => compactFmt.format(v) },
        splitLine: { show: false },
      },
    ],
    series: lines,
  });
}

// --- Dashboard renderers -----------------------------------------------------
// All three guard empty input with showEmpty so a sparse lane+window shows the empty
// state, never a blank or broken axis.

const DOW_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

// Horizontal bar from [{label, count}], coloured by the active lane. Used for the
// sub-niche / category / topic / ratio-band charts (data is pre-ordered). Every call
// site passes a VIDEO COUNT as the value, so the x-axis is named "videos".
export function renderBars(el, items, lane) {
  if (!items || !items.length) {
    showEmpty(el, "No data for this window.");
    return;
  }
  const t = THEMES[themeName()];
  const inst = mount(el);
  const color = LANE_COLORS[lane] || LANE_COLORS.health;
  inst.setOption({
    animation: !prefersReducedMotion,
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    grid: { left: 8, right: 24, top: 12, bottom: 40, containLabel: true },
    xAxis: {
      type: "value",
      minInterval: 1,
      name: "videos",
      nameLocation: "middle",
      nameGap: 28,
      nameTextStyle: { color: t.textStyle.color },
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color },
      splitLine: { lineStyle: { color: t.splitLine } },
    },
    yAxis: {
      type: "category",
      inverse: true, // largest (items[0]) on top
      data: items.map((d) => d.label),
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color, formatter: (v) => truncate(v, 24) },
    },
    series: [
      {
        type: "bar",
        data: items.map((d) => d.count),
        itemStyle: { color, borderRadius: [0, 4, 4, 0] },
      },
    ],
  });
}

// Top breakout videos as bars whose LENGTH is the views-to-subs ratio (the metric),
// biggest on top. rows are leaderboard rows (already sorted DESC by ratio).
export function renderTopRatio(el, rows, lane) {
  const data = (rows || []).filter((r) => r && r.views_to_subs_ratio != null);
  if (!data.length) {
    showEmpty(el, "No data for this window.");
    return;
  }
  const t = THEMES[themeName()];
  const inst = mount(el);
  const color = LANE_COLORS[lane] || LANE_COLORS.health;
  inst.setOption({
    animation: !prefersReducedMotion,
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (params) => {
        const r = data[params[0].dataIndex];
        return (
          `<strong>${escapeHtml(truncate(r.title || r.video_id, 50))}</strong><br/>` +
          `${Number(r.views_to_subs_ratio).toFixed(1)}x views per subscriber`
        );
      },
    },
    grid: { left: 8, right: 24, top: 12, bottom: 40, containLabel: true },
    xAxis: {
      type: "value",
      name: "views per subscriber",
      nameLocation: "middle",
      nameGap: 28,
      nameTextStyle: { color: t.textStyle.color },
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color, formatter: (v) => `${v}x` },
      splitLine: { lineStyle: { color: t.splitLine } },
    },
    yAxis: {
      type: "category",
      inverse: true, // biggest ratio on top
      data: data.map((r) => truncate(r.title || r.video_id, 24)),
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color },
    },
    series: [
      {
        type: "bar",
        data: data.map((r) => Number(r.views_to_subs_ratio)),
        itemStyle: { color, borderRadius: [0, 4, 4, 0] },
      },
    ],
  });
}

// Like-vs-comment scatter, one point per [{like_count, comment_count}].
export function renderScatter(el, pairs, lane) {
  if (!pairs || !pairs.length) {
    showEmpty(el, "No data for this window.");
    return;
  }
  const t = THEMES[themeName()];
  const inst = mount(el);
  const color = LANE_COLORS[lane] || LANE_COLORS.health;
  inst.setOption({
    animation: !prefersReducedMotion,
    tooltip: {
      trigger: "item",
      formatter: (p) =>
        `likes ${intFmt.format(p.value[0])}<br/>comments ${intFmt.format(p.value[1])}`,
    },
    grid: { left: 8, right: 24, top: 16, bottom: 36, containLabel: true },
    xAxis: {
      type: "value",
      name: "likes",
      nameLocation: "middle",
      nameGap: 28,
      nameTextStyle: { color: t.textStyle.color },
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color },
      splitLine: { lineStyle: { color: t.splitLine } },
    },
    yAxis: {
      type: "value",
      name: "comments",
      nameTextStyle: { color: t.textStyle.color },
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color },
      splitLine: { lineStyle: { color: t.splitLine } },
    },
    series: [
      {
        type: "scatter",
        symbolSize: 8,
        itemStyle: { color, opacity: 0.7 },
        data: pairs.map((p) => [p.like_count, p.comment_count]),
      },
    ],
  });
}

// Publish-time heatmap: x = hour 0-23, y = weekday (Mon-Sun), value = count. Eastern.
export function renderHeatmap(el, points, lane) {
  if (!points || !points.length) {
    showEmpty(el, "No data for this window.");
    return;
  }
  const t = THEMES[themeName()];
  const inst = mount(el);
  const color = LANE_COLORS[lane] || LANE_COLORS.health;
  const hours = Array.from({ length: 24 }, (_, h) => String(h));
  const maxCount = Math.max(...points.map((p) => p.count), 1);
  inst.setOption({
    animation: !prefersReducedMotion,
    tooltip: {
      position: "top",
      formatter: (p) =>
        `${DOW_LABELS[p.value[1]]} ${p.value[0]}:00 ET<br/>` +
        `${p.value[2]} video${p.value[2] === 1 ? "" : "s"}`,
    },
    grid: { left: 8, right: 16, top: 12, bottom: 48, containLabel: true },
    xAxis: {
      type: "category",
      data: hours,
      splitArea: { show: true },
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color },
    },
    yAxis: {
      type: "category",
      data: DOW_LABELS,
      inverse: true, // Monday on top
      splitArea: { show: true },
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color },
    },
    visualMap: {
      min: 0,
      max: maxCount,
      calculable: false,
      orient: "horizontal",
      left: "center",
      bottom: 0,
      inRange: { color: [t.splitLine, color] },
      textStyle: { color: t.textStyle.color },
    },
    series: [
      {
        type: "heatmap",
        data: points.map((p) => [p.hour, p.day_of_week, p.count]),
        label: { show: false },
        itemStyle: { borderColor: "rgba(0,0,0,0.12)", borderWidth: 1 },
      },
    ],
  });
}

// Duration histogram on a FIXED 0-180s axis. The catalog is shorts-only, so the axis
// is always 0:00-3:00 (ticks every 30s) regardless of the data's range -- the chart
// reads as "out of 3 minutes" even on a sub-60s-only day. One category per bin (all
// always emitted) pins the span AND lets ECharts auto-size the bars (no value-axis
// barWidth fragility).
const DURATION_BIN_SECONDS = 10; // bin width; 5 reads finer, 10 cleaner
const DURATION_MAX_SECONDS = 180; // the shorts cap; the axis is always 0..180

function mmss(sec) {
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}

export function renderDurationHistogram(el, durations, lane) {
  if (!durations || !durations.length) {
    showEmpty(el, "No data for this window.");
    return;
  }
  const t = THEMES[themeName()];
  const inst = mount(el);
  const color = LANE_COLORS[lane] || LANE_COLORS.health;

  const nBins = Math.ceil(DURATION_MAX_SECONDS / DURATION_BIN_SECONDS);
  const counts = new Array(nBins).fill(0);
  for (const s of durations) {
    if (s == null) continue;
    // Inclusive top edge: a 180s video lands in the last bin, never a phantom bin.
    const idx = Math.min(Math.floor(s / DURATION_BIN_SECONDS), nBins - 1);
    if (idx >= 0) counts[idx] += 1;
  }
  const binStarts = counts.map((_, i) => i * DURATION_BIN_SECONDS);

  inst.setOption({
    animation: !prefersReducedMotion,
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (params) => {
        const i = params[0].dataIndex;
        const lo = binStarts[i];
        const hi = Math.min(lo + DURATION_BIN_SECONDS, DURATION_MAX_SECONDS);
        const c = params[0].value;
        return `${mmss(lo)}-${mmss(hi)}<br/>${c} video${c === 1 ? "" : "s"}`;
      },
    },
    grid: { left: 8, right: 16, top: 12, bottom: 28, containLabel: true },
    xAxis: {
      type: "category",
      data: binStarts,
      axisLine: { lineStyle: { color: t.axisLine } },
      axisTick: { show: false },
      // Label only the 30s marks in mm:ss; blank elsewhere so the scale reads 0..3min.
      axisLabel: {
        color: t.textStyle.color,
        interval: 0,
        formatter: (v) => (Number(v) % 30 === 0 ? mmss(Number(v)) : ""),
      },
    },
    yAxis: {
      type: "value",
      minInterval: 1,
      name: "videos",
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color },
      splitLine: { lineStyle: { color: t.splitLine } },
    },
    series: [
      {
        type: "bar",
        data: counts,
        barWidth: "98%", // touch into a continuous histogram (category band percent)
        itemStyle: { color, borderRadius: [3, 3, 0, 0] },
      },
    ],
  });
}

// --- Lifecycle: view growth over time ----------------------------------------
// One line per cohort video on a SINGLE views axis (no second velocity axis: that
// is its own Velocity bar chart now). Real "Jun 19" date ticks, compact view
// labels, emoji-safe scrolling legend, stable per-video colour.
let growthSlots = {};

export function renderGrowthLines(el, payload, lane) {
  const series = ((payload && payload.series) || []).filter(
    (s) => s.points && s.points.length
  );
  if (!series.length) {
    showEmpty(el, "Not enough snapshot history yet for growth curves.");
    return;
  }
  const t = THEMES[themeName()];
  const inst = mount(el);
  growthSlots = allocateSlots(series.map((s) => s.video_id), growthSlots);

  const linkByName = {};
  const lines = series.map((s) => {
    const color = paletteColor(growthSlots[s.video_id]);
    const name = s.title || s.video_id;
    linkByName[name] = s.link || null;
    return {
      name,
      type: "line",
      showSymbol: true,
      symbolSize: 6,
      itemStyle: { color },
      lineStyle: { color },
      data: s.points.map((p) => [p.captured_at, p.view_count]),
    };
  });

  inst.setOption({
    animation: !prefersReducedMotion,
    tooltip: {
      trigger: "axis",
      formatter: (params) =>
        `<strong>${escapeHtml(fmtDateTick(params[0].axisValue))}</strong><br/>` +
        params
          .map(
            (p) =>
              `${p.marker}${videoTitleLink(p.seriesName, linkByName[p.seriesName])}: ` +
              `${compactFmt.format(p.value[1])}`
          )
          .join("<br/>"),
    },
    legend: {
      textStyle: { color: t.textStyle.color },
      top: 0,
      type: "scroll",
      formatter: (name) => truncate(name, 40),
    },
    grid: { left: 16, right: 16, top: 36, bottom: 40, containLabel: true },
    xAxis: {
      type: "time",
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color, formatter: (v) => fmtDateTick(v) },
    },
    yAxis: {
      type: "value",
      name: "views",
      nameLocation: "middle",
      nameGap: 48,
      nameTextStyle: { color: t.textStyle.color },
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color, formatter: (v) => compactFmt.format(v) },
      splitLine: { lineStyle: { color: t.splitLine } },
    },
    series: lines,
  });
}

// --- Lifecycle: peak velocity (views/day) ------------------------------------
// Horizontal bars, one per cohort video, sorted desc by PEAK views/day (the high
// point of the views-per-day curve, the SAME value the cohort selector and the
// median_peak_velocity card use). A "who peaked highest" summary beside the
// fall-off curve; reads the helper's velocity series, never recomputes.
export function renderVelocityBars(el, payload, lane) {
  const rows = ((payload && payload.series) || [])
    .map((s) => {
      const v =
        s.velocity && s.velocity.length
          ? Math.max(...s.velocity.map((p) => p.views_per_day))
          : null;
      return v == null
        ? null
        : { label: s.title || s.video_id, value: v, link: s.link || null };
    })
    .filter(Boolean)
    .sort((a, b) => b.value - a.value);
  if (!rows.length) {
    showEmpty(el, "Not enough snapshot history yet for velocity.");
    return;
  }
  const t = THEMES[themeName()];
  const inst = mount(el);
  const color = LANE_COLORS[lane] || LANE_COLORS.health;

  inst.setOption({
    animation: !prefersReducedMotion,
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (ps) => {
        const r = rows[ps[0].dataIndex] || {};
        return (
          `${videoTitleLink(r.label, r.link)}<br/>` +
          `${intFmt.format(Math.round(ps[0].value))} peak views/day`
        );
      },
    },
    grid: { left: 8, right: 24, top: 12, bottom: 36, containLabel: true },
    xAxis: {
      type: "value",
      name: "peak views/day",
      nameLocation: "middle",
      nameGap: 28,
      nameTextStyle: { color: t.textStyle.color },
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color, formatter: (v) => compactFmt.format(v) },
      splitLine: { lineStyle: { color: t.splitLine } },
    },
    yAxis: {
      type: "category",
      inverse: true,
      data: rows.map((r) => r.label),
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color, formatter: (v) => truncate(v, 28) },
    },
    series: [
      {
        type: "bar",
        data: rows.map((r) => Math.round(r.value)),
        itemStyle: { color, borderRadius: [0, 4, 4, 0] },
      },
    ],
  });
}

// --- Lifecycle: views per day over time (the fall-off curve) -----------------
// One line per growth-cohort video, x = date, y = views/day on a single axis,
// over the full tracked span. This is the rise-AND-fall view that cumulative
// View growth cannot show (cumulative views only flatten, never descend). Plots
// the helper's existing `velocity` series (a pure replot of the same
// _velocity_points the cohort selector and the peak bars/card read), so a line's
// visible high point equals that video's bar height and its peak-velocity card
// contribution. Same scroll-legend, stable slot colour, and tooltip-link
// structure as renderGrowthLines.
let velocityLineSlots = {};

export function renderVelocityLines(el, payload, lane, limit) {
  // payload.series is peak-sorted, so the first `limit` are the highest-peak
  // videos. limit is a user-set view control (the curve overplots at the full
  // cohort size); null/0 means show every series with a velocity.
  const all = ((payload && payload.series) || []).filter(
    (s) => s.velocity && s.velocity.length
  );
  const series = limit ? all.slice(0, limit) : all;
  if (!series.length) {
    showEmpty(el, "Not enough snapshot history yet for velocity curves.");
    return;
  }
  const t = THEMES[themeName()];
  const inst = mount(el);
  velocityLineSlots = allocateSlots(series.map((s) => s.video_id), velocityLineSlots);

  const linkByName = {};
  const lines = series.map((s) => {
    const color = paletteColor(velocityLineSlots[s.video_id]);
    const name = s.title || s.video_id;
    linkByName[name] = s.link || null;
    return {
      name,
      type: "line",
      showSymbol: true,
      symbolSize: 5,
      itemStyle: { color },
      lineStyle: { color },
      data: s.velocity.map((p) => [p.captured_at, p.views_per_day]),
    };
  });

  inst.setOption({
    animation: !prefersReducedMotion,
    tooltip: {
      trigger: "axis",
      formatter: (params) =>
        `<strong>${escapeHtml(fmtDateTick(params[0].axisValue))}</strong><br/>` +
        params
          .map(
            (p) =>
              `${p.marker}${videoTitleLink(p.seriesName, linkByName[p.seriesName])}: ` +
              `${compactFmt.format(Math.round(p.value[1]))}/day`
          )
          .join("<br/>"),
    },
    legend: {
      textStyle: { color: t.textStyle.color },
      top: 0,
      type: "scroll",
      formatter: (name) => truncate(name, 40),
    },
    grid: { left: 16, right: 16, top: 36, bottom: 40, containLabel: true },
    xAxis: {
      type: "time",
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color, formatter: (v) => fmtDateTick(v) },
    },
    yAxis: {
      type: "value",
      name: "views/day",
      nameLocation: "middle",
      nameGap: 48,
      nameTextStyle: { color: t.textStyle.color },
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color, formatter: (v) => compactFmt.format(v) },
      splitLine: { lineStyle: { color: t.splitLine } },
    },
    series: lines,
  });
}

// --- Lifecycle: rank movement (survivors' rank across runs) ------------------
// Lifecycle-specific (NOT renderBump, which is Trends' and paints a fell-off line
// RED; in an all-time lifecycle view every cohort video has fallen off, so that
// would make every line red and indistinguishable). Here each video gets a
// distinct palette colour, a labelled scrolling legend, short "Jun 8" date ticks,
// and a rank axis with 1 at the top. Empty-state when no video has >=2 runs.
let rankSlots = {};

export function renderRankMovement(el, data, lane) {
  const runDates = (data && data.run_dates) || [];
  const series = ((data && data.series) || []).filter(
    (s) => s.points && s.points.length >= 2
  );
  if (!runDates.length || !series.length) {
    showEmpty(el, "Not enough repeat appearances to show rank movement.");
    return;
  }
  const t = THEMES[themeName()];
  const inst = mount(el);
  rankSlots = allocateSlots(series.map((s) => s.video_id), rankSlots);

  const linkByName = {};
  const lines = series.map((s) => {
    const byDate = {};
    for (const p of s.points) byDate[p.run_date] = p.rank;
    const color = paletteColor(rankSlots[s.video_id]);
    const name = s.title || s.video_id;
    linkByName[name] = s.link || null;
    return {
      name,
      type: "line",
      connectNulls: false,
      showSymbol: true,
      symbolSize: 7,
      itemStyle: { color },
      lineStyle: { color },
      data: runDates.map((d) => (d in byDate ? byDate[d] : null)),
    };
  });

  inst.setOption({
    animation: !prefersReducedMotion,
    tooltip: {
      trigger: "item",
      formatter: (p) =>
        `<strong>${videoTitleLink(p.seriesName, linkByName[p.seriesName])}</strong><br/>` +
        `rank ${p.value} on ${escapeHtml(fmtRunDate(p.name))}`,
    },
    legend: {
      textStyle: { color: t.textStyle.color },
      top: 0,
      type: "scroll",
      formatter: (name) => truncate(name, 36),
    },
    grid: { ...baseGrid, top: 36 },
    ...axes(t, {
      xAxis: {
        type: "category",
        data: runDates,
        boundaryGap: false,
        axisLabel: { color: t.textStyle.color, formatter: (v) => fmtRunDate(v) },
      },
      yAxis: { type: "value", inverse: true, minInterval: 1, name: "rank (1 = best)" },
    }),
    series: lines,
  });
}

// --- Lifecycle: like:comment ratio over time ---------------------------------
// Multi-line, modelled on the trajectory's line/legend structure but with a
// SINGLE "ratio" y-axis (trajectory's second views/day axis is wrong here). Stable
// per-video colour so a line keeps its colour as the cohort shifts run to run.
let ratioSlots = {};

export function renderRatioLines(el, series, lane) {
  const withData = (series || []).filter((s) => s.points && s.points.length);
  if (!withData.length) {
    showEmpty(el, "No like-to-comment history for this window yet.");
    return;
  }
  const t = THEMES[themeName()];
  const inst = mount(el);
  ratioSlots = allocateSlots(withData.map((s) => s.video_id), ratioSlots);

  const linkByName = {};
  const lines = withData.map((s) => {
    const color = paletteColor(ratioSlots[s.video_id]);
    const name = s.title || s.video_id;
    linkByName[name] = s.link || null;
    return {
      name,
      type: "line",
      showSymbol: true,
      symbolSize: 6,
      itemStyle: { color },
      lineStyle: { color },
      data: s.points.map((p) => [p.captured_at, p.ratio]),
    };
  });

  inst.setOption({
    animation: !prefersReducedMotion,
    tooltip: {
      trigger: "axis",
      formatter: (params) =>
        `<strong>${escapeHtml(fmtDateTick(params[0].axisValue))}</strong><br/>` +
        params
          .map(
            (p) =>
              `${p.marker}${videoTitleLink(p.seriesName, linkByName[p.seriesName])}: ` +
              `${p.value[1].toFixed(1)}`
          )
          .join("<br/>"),
    },
    legend: {
      textStyle: { color: t.textStyle.color },
      top: 0,
      type: "scroll",
      formatter: (name) => truncate(name, 40),
    },
    grid: { ...baseGrid, top: 36 },
    xAxis: {
      type: "time",
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color, formatter: (v) => fmtDateTick(v) },
    },
    yAxis: {
      type: "value",
      name: "likes per comment",
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color },
      splitLine: { lineStyle: { color: t.splitLine } },
    },
    series: lines,
  });
}

// --- Lifecycle: engagement maturation (ratio vs age, coloured by views) -------
// Current-state cross-section: one small dot per in-window video, x = age in days,
// y = likes-per-comment on a LOG axis (a 1340 outlier over a ~74 median would flatten
// a linear axis; log keeps every point on-chart, no axis cap so nothing is dropped).
// View count is encoded by COLOUR via visualMap, not by giant bubble sizes (those
// overlapped into blobs). Null/zero ratios are dropped (log needs positive y).
export function renderMaturation(el, points, lane) {
  const pts = (points || []).filter((p) => p.ratio != null && p.ratio > 0);
  if (!pts.length) {
    showEmpty(el, "No data for this window.");
    return;
  }
  const t = THEMES[themeName()];
  const inst = mount(el);
  const views = pts.map((p) => p.view_count || 0);
  const vmin = Math.min(...views);
  const vmax = Math.max(...views);

  inst.setOption({
    animation: !prefersReducedMotion,
    tooltip: {
      trigger: "item",
      formatter: (p) =>
        `<strong>${videoTitleLink(p.value[3], p.value[4])}</strong><br/>` +
        `${p.value[0].toFixed(1)} days old<br/>` +
        `${p.value[1].toFixed(1)} likes per comment<br/>` +
        `${intFmt.format(p.value[2])} views`,
    },
    visualMap: {
      type: "continuous",
      min: vmin,
      max: vmax > vmin ? vmax : vmin + 1,
      dimension: 2,
      calculable: true,
      orient: "horizontal",
      left: "center",
      bottom: 0,
      itemWidth: 12,
      text: ["more views", "fewer"],
      textStyle: { color: t.textStyle.color },
      formatter: (v) => compactFmt.format(v),
      inRange: { color: ["#2D7CFF", "#00F2A9"] },
    },
    grid: { left: 8, right: 24, top: 16, bottom: 58, containLabel: true },
    xAxis: {
      type: "value",
      name: "days since publish",
      nameLocation: "middle",
      nameGap: 28,
      nameTextStyle: { color: t.textStyle.color },
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color },
      splitLine: { lineStyle: { color: t.splitLine } },
    },
    yAxis: {
      type: "log",
      name: "likes per comment (log)",
      nameLocation: "middle",
      nameGap: 40,
      nameTextStyle: { color: t.textStyle.color },
      axisLine: { lineStyle: { color: t.axisLine } },
      axisLabel: { color: t.textStyle.color },
      splitLine: { lineStyle: { color: t.splitLine } },
    },
    series: [
      {
        type: "scatter",
        symbolSize: 9,
        itemStyle: { opacity: 0.78, borderColor: t.splitLine, borderWidth: 0.5 },
        // value carries title + link in dims 3/4 so the tooltip can link the video
        // (visualMap reads dim 2; the extra dims are tooltip-only).
        data: pts.map((p) => [
          p.days_since_publish,
          p.ratio,
          p.view_count,
          p.title || p.video_id,
          p.link || null,
        ]),
      },
    ],
  });
}
