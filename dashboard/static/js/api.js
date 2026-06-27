// Thin fetch wrappers over the Phase 0/1 read endpoints. No state, no DOM.

async function getJSON(url) {
  const resp = await fetch(url, { headers: { Accept: "application/json" } });
  if (!resp.ok) {
    throw new Error(`${resp.status} ${resp.statusText} for ${url}`);
  }
  return resp.json();
}

// Build an Error from a non-ok response, preferring the server's {detail} (FastAPI's
// clean message — "max_tokens must be <= 8192", "max_tokens is required ...", a
// 422/409/400) and falling back to the status line when the body is not JSON (e.g. a
// proxy page). Shared by POST and PUT so an edited-row error surfaces the same honest
// message the add form does, not a bare "422 Unprocessable Content".
export async function errorFrom(resp) {
  let detail = `${resp.status} ${resp.statusText}`;
  try {
    const b = await resp.json();
    if (b && b.detail) detail = b.detail;
  } catch (_) { /* non-JSON body: keep the status line */ }
  return new Error(detail);
}

async function putJSON(url, body) {
  const resp = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw await errorFrom(resp);
  return resp.json();
}

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw await errorFrom(resp);
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

// Whether a discover has already completed today (Pacific). Drives the Extract
// button's label; the cap itself is enforced server-side in the pipeline resolver.
export async function getRunState() {
  const data = await getJSON("/api/run-state");
  return !!(data && data.discover_ran_today);
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

export function getRankHistory(lane, videoIds, startDate, endDate) {
  // start_date/end_date go in the filters arg (not base) so buildQuery drops them
  // when null/undefined; the bump chart windows to the date filter when set.
  const qs = buildQuery(
    { lane },
    { video_id: videoIds, start_date: startDate, end_date: endDate }
  );
  return getJSON(`/api/rank-history?${qs}`);
}

export function getDistribution(runDate, lane) {
  const qs = buildQuery({ run_date: runDate, lane }, null);
  return getJSON(`/api/distribution?${qs}`);
}

// --- Dashboard tab: lane-scoped, period-aggregated reads ---------------------
// start_date/end_date go in the filters arg so buildQuery drops them when null
// (all time), exactly like getRankHistory.
export function getDashboardTopicMix(lane, startDate, endDate) {
  const qs = buildQuery({ lane }, { start_date: startDate, end_date: endDate });
  return getJSON(`/api/dashboard/topic-mix?${qs}`);
}

export function getDashboardBreakout(lane, startDate, endDate) {
  const qs = buildQuery({ lane }, { start_date: startDate, end_date: endDate });
  return getJSON(`/api/dashboard/breakout?${qs}`);
}

export function getDashboardFormat(lane, startDate, endDate) {
  const qs = buildQuery({ lane }, { start_date: startDate, end_date: endDate });
  return getJSON(`/api/dashboard/format?${qs}`);
}

// --- Phase B: read-only interpretation --------------------------------------

// scope IS the lane bucket (health / habit / overall), an identity mapping. The
// endpoint returns 200 with empty text when no row exists; callers treat that as
// the empty state, not an error.
export function getInterpretation(runDate, lane) {
  const qs = buildQuery({ run_date: runDate, scope: lane }, null);
  return getJSON(`/api/interpretation?${qs}`);
}

// --- Phase 4: spend display -------------------------------------------------

// Two LLM-spend breakdowns: the selected run and the current month, each split by
// model. runDate is optional (omit it and the run section comes back empty). Tokens
// are exact; cost is an estimate, null for any model absent from the price map.
export function getSpend(runDate) {
  const qs = buildQuery(runDate ? { run_date: runDate } : {}, null);
  return getJSON(`/api/spend?${qs}`);
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

// --- Phase 3 generator: prepopulation + run ---------------------------------

// Model options, per-model honored-parameter map (capabilities), and the last-used
// model/temperature/seed (or defaults). One fetch populates both the dropdown and
// the current selection.
export function getInterpretDefaults() {
  return getJSON("/api/interpret-defaults");
}

// Trigger synthesis for one lane (run_date, scope=lane) with the chosen model +
// parameters. Resolves to {scope, skipped, ...} — on success also text, tokens,
// seed_applied, think_applied, thinking, model; on an empty lane just
// {scope, skipped:true}. `think` is true/false for an honoring model, or null when
// the toggle did not apply (the server normalizes either way).
export function runInterpret({ runDate, scope, model, temperature, seed, think, fields }) {
  return postJSON("/api/interpret", {
    run_date: runDate,
    scope,
    model,
    temperature,
    seed,
    think,
    fields,
  });
}

// --- Extract page: the SSE run/stream (the one streaming endpoint) -----------
// Returns the RAW Response (not getJSON): the caller checks the status (409 = a discover is
// already running, 422 = a bad mode) and, on 200, reads the body as a text/event-stream.
// `signal` is a per-run AbortController signal for teardown.
export function streamExtract(mode, signal) {
  return fetch(`/api/extract/stream?${new URLSearchParams({ mode })}`, { signal });
}

// The classifier model picker: the dropdown options + the current effective primary /
// fallback, and the paired persist. setClassificationModels goes through postJSON so a
// 422 detail ("... must use a DIFFERENT provider ...") surfaces inline.
export function getClassificationModels() {
  return getJSON("/api/extract/models");
}

export function setClassificationModels(primary, fallback) {
  return postJSON("/api/extract/models", { primary, fallback });
}

// --- Phase 5: the /models registry editor -----------------------------------
// model strings carry ':' and '.', so every path segment is encodeURIComponent'd.
// Writes go through postJSON/putJSON so a server `detail` (e.g. "valid_from must be
// after ...") surfaces inline. Delete/restore are bodyless POSTs.

const seg = encodeURIComponent;

export function getModels(includeDeleted = false) {
  const qs = includeDeleted ? "?include_deleted=true" : "";
  return getJSON(`/api/models${qs}`);
}

export function insertModel(body) {
  return postJSON("/api/models", body);
}

export function updateModel(model, body) {
  return putJSON(`/api/models/${seg(model)}`, body);
}

export function deleteModel(model) {
  return postJSON(`/api/models/${seg(model)}/delete`, {});
}

export function restoreModel(model) {
  return postJSON(`/api/models/${seg(model)}/restore`, {});
}

export function getModelInvocationCount(model) {
  return getJSON(`/api/models/${seg(model)}/invocation-count`);
}

export function getPrices(model, includeDeleted = false) {
  const qs = includeDeleted ? "?include_deleted=true" : "";
  return getJSON(`/api/models/${seg(model)}/prices${qs}`);
}

export function insertPrice(model, body) {
  return postJSON(`/api/models/${seg(model)}/prices`, body);
}

export function deletePrice(priceId) {
  return postJSON(`/api/prices/${priceId}/delete`, {});
}

export function restorePrice(priceId) {
  return postJSON(`/api/prices/${priceId}/restore`, {});
}

export function getPriceInvocationCount(priceId) {
  return getJSON(`/api/prices/${priceId}/invocation-count`);
}

// --- Documentation page: filesystem-discovered docs -------------------------
// The registry (tabs + documents) and a raw document fetch. Documents are parsed
// client-side by the per-format adapters, so the raw fetch returns text/bytes.

export function getDocumentationRegistry() {
  return getJSON("/api/documentation");
}

// Reading-mode formats (md/html) are fetched as text for the string adapters.
export async function getDocumentRaw(tabId, docId) {
  const qs = buildQuery({ tab: tabId, doc: docId }, null);
  const resp = await fetch(`/api/documentation/raw?${qs}`);
  if (!resp.ok) {
    throw new Error(`${resp.status} ${resp.statusText}`);
  }
  return resp.text();
}

// Native-mode formats (docx) are binary: fetch the same endpoint as an ArrayBuffer so
// docx-preview can parse the bytes. Same URL, the body is just read as bytes not text.
export async function getDocumentBytes(tabId, docId) {
  const qs = buildQuery({ tab: tabId, doc: docId }, null);
  const resp = await fetch(`/api/documentation/raw?${qs}`);
  if (!resp.ok) {
    throw new Error(`${resp.status} ${resp.statusText}`);
  }
  return resp.arrayBuffer();
}

// --- Price-refresh agent (Phase 3) ------------------------------------------
// Reads + writes go through getJSON/postJSON + errorFrom, so a server {detail}
// (e.g. "unknown or unusable model 'x'") surfaces as the inline message.

export function getPriceRefresh() {
  return getJSON("/api/price-refresh");
}

export function getExtractionModel() {
  return getJSON("/api/extraction-model");
}

export function setExtractionModel(model) {
  return postJSON("/api/extraction-model", { model });
}

export function confirmProposal(id) {
  return postJSON(`/api/price-proposals/${id}/confirm`, {});
}

export function rejectProposal(id) {
  return postJSON(`/api/price-proposals/${id}/reject`, {});
}

export function runPriceRefresh() {
  return postJSON("/api/price-refresh/run", {});
}
