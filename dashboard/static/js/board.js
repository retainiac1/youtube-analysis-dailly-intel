// Renders the leaderboard rows, run selector, and empty states. Presentation
// only; main.js owns state and wiring.

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

const intFmt = new Intl.NumberFormat();

function fmtInt(n) {
  return n == null ? "—" : intFmt.format(n);
}

function fmtRatio(n) {
  return n == null ? "—" : Number(n).toFixed(1);
}

function fmtDate(s) {
  if (!s) return "—";
  const m = /^\d{4}-\d{2}-\d{2}/.exec(s);
  return m ? m[0] : s;
}

export function populateRunSelect(select, runDates) {
  select.replaceChildren();
  for (const d of runDates) {
    select.appendChild(el("option", { value: d, text: d }));
  }
  if (runDates.length) select.value = runDates[0];
}

export function setActiveTab(app, tabs, lane) {
  app.dataset.lane = lane;
  for (const tab of tabs) {
    const active = tab.dataset.lane === lane;
    tab.setAttribute("aria-selected", active ? "true" : "false");
    tab.tabIndex = active ? 0 : -1;
  }
}

function stat(label, value) {
  return el("div", { class: "stat" }, [
    el("span", { class: "stat-value", text: value }),
    el("span", { class: "stat-label", text: label }),
  ]);
}

function rankBadge(rank) {
  return el("div", { class: "rank", text: rank != null ? `#${rank}` : "—" });
}

// The "track" affordance adds a video to the Trends trajectory chart. It is
// ephemeral in-memory view state (main.js state.tracked) — NOT a DB write and not
// related to starred. When the 5-video cap is reached, untracked rows show a
// disabled "5 max" state rather than silently ignoring a sixth click.
function trackButton(row, trackState) {
  if (!row.video_id) return null; // a ghost ranking has nothing to track
  const isTracked = trackState.tracked.has(row.video_id);
  const full = trackState.full && !isTracked;
  const btn = el("button", {
    class: "track-btn",
    type: "button",
    "data-track": row.video_id,
    "aria-pressed": isTracked ? "true" : "false",
    text: full ? "5 max" : isTracked ? "Tracking" : "Track",
    title: full
      ? "Tracking 5 videos already (the max). Untrack one first."
      : isTracked
      ? "Stop charting this video"
      : "Chart this video in Trends",
  });
  if (full) btn.disabled = true;
  return btn;
}

function rowNode(row, trackState) {
  const hasVideo = row.title != null || row.link != null;
  const link = row.link || null;

  const thumb = link
    ? el("a", { class: "thumb", href: link, target: "_blank", rel: "noopener" }, [
        row.thumbnail_url
          ? el("img", { src: row.thumbnail_url, alt: "", loading: "lazy" })
          : el("div", { class: "thumb-missing" }),
      ])
    : el("div", { class: "thumb thumb-missing" });

  const title = link
    ? el("a", { class: "row-title", href: link, target: "_blank", rel: "noopener",
        text: row.title || "(untitled)" })
    : el("span", { class: "row-title row-title-missing",
        text: hasVideo ? row.title || "(untitled)" : "(video not found)" });

  const metric = row.metric_value != null ? row.metric_value : row.views_to_subs_ratio;

  const stats = el("div", { class: "row-stats" }, [
    stat("ratio", fmtRatio(metric)),
    stat("views", fmtInt(row.view_count)),
    stat("subs", fmtInt(row.subscriber_count)),
    stat("/day", fmtRatio(row.views_per_day)),
    stat("published", fmtDate(row.published_at)),
  ]);

  const main = el("div", { class: "row-main" }, [
    title,
    el("div", { class: "row-channel", text: row.channel_title || "—" }),
    stats,
  ]);

  return el("article", { class: "row" }, [
    rankBadge(row.rank),
    thumb,
    main,
    trackButton(row, trackState),
  ]);
}

export function renderRows(boardEl, data, lane, trackState) {
  boardEl.replaceChildren();
  const rows = data.rows || [];
  if (!rows.length) {
    boardEl.appendChild(el("div", { class: "empty-state" }, [
      el("p", { class: "heading-sm",
        text: "No videos match these filters for this run." }),
      el("p", { text: "Loosen a filter or pick another day." }),
    ]));
    return;
  }
  const list = el("div", { class: "row-list" },
    rows.map((row) => rowNode(row, trackState)));
  boardEl.appendChild(list);
}

export function renderNoRuns(boardEl) {
  boardEl.replaceChildren(el("div", { class: "empty-state" }, [
    el("p", { class: "heading-sm", text: "No runs yet." }),
    el("p", { text: "The leaderboard fills in once the pipeline records a run." }),
  ]));
}

export function renderError(boardEl, message) {
  boardEl.replaceChildren(el("div", { class: "empty-state empty-error" }, [
    el("p", { class: "heading-sm", text: "Could not load the board." }),
    el("p", { text: message }),
  ]));
}
