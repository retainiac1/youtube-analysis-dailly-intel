// Thin fetch wrappers over the Phase 0/1 read endpoints. No state, no DOM.

async function getJSON(url) {
  const resp = await fetch(url, { headers: { Accept: "application/json" } });
  if (!resp.ok) {
    throw new Error(`${resp.status} ${resp.statusText} for ${url}`);
  }
  return resp.json();
}

async function putJSON(url, body) {
  const resp = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    throw new Error(`${resp.status} ${resp.statusText} for ${url}`);
  }
  return resp.json();
}

// Serialize {run_date, lane} plus a filters object into a query string.
// Arrays append one param per value (the repeatable-param contract the backend
// expects); booleans append only when true; null / "" are omitted.
export function buildQuery(base, filters) {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(base)) sp.append(k, v);
  for (const [k, v] of Object.entries(filters || {})) {
    if (v == null) continue;
    if (Array.isArray(v)) {
      v.forEach((x) => sp.append(k, x));
    } else if (typeof v === "boolean") {
      if (v) sp.append(k, "true");
    } else if (v === "") {
      continue;
    } else {
      sp.append(k, v);
    }
  }
  return sp.toString();
}

export async function getRuns() {
  const data = await getJSON("/api/runs");
  return data.run_dates || [];
}

export function getFilterOptions(runDate, lane) {
  const qs = buildQuery({ run_date: runDate, lane }, null);
  return getJSON(`/api/filter-options?${qs}`);
}

export function getLibrary(runDate, lane, filters) {
  const qs = buildQuery({ run_date: runDate, lane }, filters);
  return getJSON(`/api/library?${qs}`);
}

// --- Phase 2: change-over-time charts ---------------------------------------

export function getSnapshots(videoIds) {
  const qs = buildQuery({}, { video_id: videoIds });
  return getJSON(`/api/snapshots?${qs}`);
}

export function getRankHistory(lane, videoIds) {
  const qs = buildQuery({ lane }, { video_id: videoIds });
  return getJSON(`/api/rank-history?${qs}`);
}

export function getDistribution(runDate, lane) {
  const qs = buildQuery({ run_date: runDate, lane }, null);
  return getJSON(`/api/distribution?${qs}`);
}

// --- Phase B: read-only interpretation --------------------------------------

// scope IS the lane bucket (health / habit / overall), an identity mapping. The
// endpoint returns 200 with empty text when no row exists; callers treat that as
// the empty state, not an error.
export function getInterpretation(runDate, lane) {
  const qs = buildQuery({ run_date: runDate, scope: lane }, null);
  return getJSON(`/api/interpretation?${qs}`);
}

// --- Phase 3: the only writes (user_notes + starred) ------------------------

export function setNotes(videoId, userNotes) {
  return putJSON(
    `/api/videos/${encodeURIComponent(videoId)}/notes`,
    { user_notes: userNotes },
  );
}

export function setStarred(videoId, starred) {
  return putJSON(
    `/api/videos/${encodeURIComponent(videoId)}/star`,
    { starred },
  );
}
