// Trends view orchestration: fetches chart data via api.js and dispatches to the
// chart renderers in charts.js. Caches the last payloads so a theme toggle can
// redraw without refetching.
//
// Scoping (see the plan): the bump chart and trajectory span the current LANE across
// all runs (the run picker does not constrain them). A lane change refreshes both; a
// tracked-set change refreshes only the trajectory. (The view-count histogram moved to
// the Dashboard as a period-aware card in Phase 2.)

import * as api from "./api.js";
import * as charts from "./charts.js";

let els = null;
const cache = { rankHistory: null, snapshots: null, lane: "health" };

export function init(elements) {
  els = elements;
}

export async function refreshBump(state) {
  cache.lane = state.lane;
  // Window the bump chart to the date filter (server-side run_date bounds). With
  // fewer than two runs in the window, charts.renderBump shows its empty state.
  cache.rankHistory = await api.getRankHistory(
    state.lane, [], state.startDate, state.endDate
  );
  charts.renderBump(els.bump, cache.rankHistory, state.lane);
}

// Trim each snapshot series to the date filter window. captured_at is an Eastern
// ISO-8601 string, so its leading YYYY-MM-DD is the Eastern calendar date; comparing
// that prefix makes end_date inclusive through end-of-day Eastern automatically. Both
// points and velocity carry captured_at and must be trimmed together so the views and
// views/day lines stay aligned.
function windowSnapshots(payload, startDate, endDate) {
  const series = (payload && payload.series) || [];
  // All-time: keep everything. Guard FIRST: null coerces to 0 in a range compare,
  // which would silently drop the wrong points.
  if (startDate == null && endDate == null) return payload;
  const inWindow = (rec) => {
    const ts = rec && rec.captured_at;
    if (typeof ts !== "string") return false; // never slice a non-string
    const day = ts.slice(0, 10);
    if (startDate != null && day < startDate) return false;
    if (endDate != null && day > endDate) return false;
    return true;
  };
  return {
    ...payload,
    series: series.map((s) => ({
      ...s,
      points: (s.points || []).filter(inWindow),
      velocity: (s.velocity || []).filter(inWindow),
    })),
  };
}

export async function refreshTrajectory(state) {
  cache.lane = state.lane;
  const ids = [...state.tracked];
  const raw = ids.length ? await api.getSnapshots(ids) : { series: [] };
  cache.snapshots = windowSnapshots(raw, state.startDate, state.endDate);
  charts.renderTrajectory(els.trajectory, cache.snapshots, state.lane);
}

// Lane change (and first mount): both charts.
export async function refreshAll(state) {
  await Promise.all([
    refreshBump(state),
    refreshTrajectory(state),
  ]);
}

// Theme toggle: redraw from cache (charts.js disposes + re-inits with the new
// theme). No refetch.
export function rerenderFromCache() {
  if (cache.rankHistory) charts.renderBump(els.bump, cache.rankHistory, cache.lane);
  charts.renderTrajectory(
    els.trajectory,
    cache.snapshots || { series: [] },
    cache.lane
  );
}
