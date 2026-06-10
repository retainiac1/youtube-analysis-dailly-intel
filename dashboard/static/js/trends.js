// Trends view orchestration: fetches chart data via api.js and dispatches to the
// chart renderers in charts.js. Caches the last payloads so a theme toggle can
// redraw without refetching.
//
// Scoping (see the plan): the histogram is the current run + lane; the bump chart
// and trajectory span the current LANE across all runs (the run picker does not
// constrain them). So a run change refreshes only the histogram; a lane change
// refreshes all three; a tracked-set change refreshes only the trajectory.

import * as api from "./api.js";
import * as charts from "./charts.js";

let els = null;
const cache = { rankHistory: null, distribution: null, snapshots: null, lane: "health" };

export function init(elements) {
  els = elements;
}

export async function refreshBump(state) {
  cache.lane = state.lane;
  cache.rankHistory = await api.getRankHistory(state.lane, []);
  charts.renderBump(els.bump, cache.rankHistory, state.lane);
}

export async function refreshHistogram(state) {
  cache.lane = state.lane;
  cache.distribution = await api.getDistribution(state.runDate, state.lane);
  charts.renderHistogram(els.distribution, cache.distribution, state.lane);
}

export async function refreshTrajectory(state) {
  cache.lane = state.lane;
  const ids = [...state.tracked];
  cache.snapshots = ids.length ? await api.getSnapshots(ids) : { series: [] };
  charts.renderTrajectory(els.trajectory, cache.snapshots, state.lane);
}

// Lane change (and first mount): all three charts.
export async function refreshAll(state) {
  await Promise.all([
    refreshBump(state),
    refreshHistogram(state),
    refreshTrajectory(state),
  ]);
}

// Theme toggle: redraw from cache (charts.js disposes + re-inits with the new
// theme). No refetch.
export function rerenderFromCache() {
  if (cache.rankHistory) charts.renderBump(els.bump, cache.rankHistory, cache.lane);
  if (cache.distribution)
    charts.renderHistogram(els.distribution, cache.distribution, cache.lane);
  charts.renderTrajectory(
    els.trajectory,
    cache.snapshots || { series: [] },
    cache.lane
  );
}
