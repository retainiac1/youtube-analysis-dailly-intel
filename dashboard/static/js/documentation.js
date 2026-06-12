// Documentation page controller. Fetches the filesystem-discovered registry, renders
// the page-scoped category tabs and the per-tab document list, then loads a selected
// document's bytes, runs the matching adapter, and hands the normalized shape to the
// presenter. Owns the #documentation region via replaceChildren (the Models pattern).
//
// These category tabs are page-scoped and deliberately SEPARATE from the global lane
// tabs (Health/Habit/Overall): their own classes, no data-lane, no shared state. The
// page is not run/lane scoped.

import * as api from "./api.js";
import { ADAPTERS } from "./doc-adapters.js";
import * as presenter from "./doc-presenter.js";

let mountEl = null;
let viewportEl = null;
let loadSeq = 0; // guards against an out-of-order document fetch winning the viewport

const state = { tabs: [], activeTabId: null, activeDocId: null };

export function init(mount) {
  mountEl = mount;
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    node.append(child instanceof Node ? child : document.createTextNode(child));
  }
  return node;
}

function notice(message) {
  return el("p", { class: "doc-notice" }, message);
}

// A document is renderable this phase only if its format has a client adapter.
function isRenderable(doc) {
  return Boolean(ADAPTERS[doc.format]);
}

function tabById(id) {
  return state.tabs.find((t) => t.tabId === id);
}

// Fetch the registry on every visit (live pickup: the server re-scans per request).
export async function refresh() {
  let registry;
  try {
    registry = await api.getDocumentationRegistry();
  } catch (err) {
    mountEl.replaceChildren(notice(`Could not load documents: ${err.message || err}`));
    return;
  }

  // Keep only tabs that have at least one document this phase can render; drop the
  // rest (a docx/pdf-only tab reappears automatically once its adapter ships).
  state.tabs = (registry.tabs || [])
    .map((tab) => ({ ...tab, docs: tab.docs.filter(isRenderable) }))
    .filter((tab) => tab.docs.length);

  if (!state.tabs.length) {
    mountEl.replaceChildren(notice("No documents found."));
    return;
  }

  // Preserve the current selection across refreshes when it still exists.
  if (!tabById(state.activeTabId)) {
    state.activeTabId = state.tabs[0].tabId;
    state.activeDocId = null;
  }
  const tab = tabById(state.activeTabId);
  if (!tab.docs.some((d) => d.docId === state.activeDocId)) {
    state.activeDocId = tab.docs[0].docId;
  }

  renderShell();
  await loadActiveDoc();
}

// Arrow-key roving across a list of tab buttons (mirrors main.js wireTablist, kept
// local so the page-scoped tabs never touch the global lane tablist).
function wireRoving(buttons, onSelect) {
  buttons.forEach((button, i) => {
    button.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      const dir = e.key === "ArrowRight" ? 1 : -1;
      const next = buttons[(i + dir + buttons.length) % buttons.length];
      next.focus();
      onSelect(next.dataset.tabId);
    });
  });
}

function renderShell() {
  const tabButtons = state.tabs.map((tab) => {
    const selected = tab.tabId === state.activeTabId;
    const button = el("button", {
      class: "doc-tab",
      type: "button",
      role: "tab",
      "data-tab-id": tab.tabId,
      "aria-selected": selected ? "true" : "false",
      tabindex: selected ? "0" : "-1",
    }, tab.tabLabel);
    button.addEventListener("click", () => selectTab(tab.tabId));
    return button;
  });
  wireRoving(tabButtons, selectTab);

  const tabs = el("nav", {
    class: "doc-tabs", role: "tablist", "aria-label": "Documentation categories",
  }, tabButtons);

  const docs = renderDocList();
  // Tabs + document list form the page's sub-nav, pinned below the global header.
  // Pinning it keeps the sticky TOC's anchor constant (its unstuck and stuck tops
  // match), so the TOC fills the band below the sub-nav without ever overshooting.
  const subnav = el("div", { class: "doc-subnav" }, [tabs, docs]);
  viewportEl = el("div", { class: "doc-viewport" });

  mountEl.replaceChildren(el("div", { class: "documentation-page" }, [subnav, viewportEl]));
  measureSubnav(subnav);
}

let subnavObserver = null;

// Publish the pinned sub-nav's live height to a CSS var so the TOC's sticky offset and
// max-height derive from it (no hardcoded nav height). Mirrors sticky-header.js's
// --header-h: set it once now and on every later resize (wrap, font load, theme).
function measureSubnav(subnav) {
  const publish = () => document.documentElement.style.setProperty(
    "--doc-subnav-h", `${subnav.getBoundingClientRect().height}px`);
  publish();
  if (subnavObserver) subnavObserver.disconnect();
  subnavObserver = new ResizeObserver(publish);
  subnavObserver.observe(subnav);
}

function renderDocList() {
  const tab = tabById(state.activeTabId);
  const buttons = tab.docs.map((doc) => {
    const active = doc.docId === state.activeDocId;
    const button = el("button", {
      class: active ? "doc-pill is-active" : "doc-pill",
      type: "button",
      "data-doc-id": doc.docId,
      "aria-current": active ? "true" : "false",
    }, doc.title);
    button.addEventListener("click", () => selectDoc(doc.docId));
    return button;
  });
  return el("nav", { class: "doc-docs", "aria-label": "Documents" }, buttons);
}

function selectTab(tabId) {
  if (tabId === state.activeTabId) return;
  state.activeTabId = tabId;
  state.activeDocId = tabById(tabId).docs[0].docId;
  renderShell();
  loadActiveDoc();
}

function selectDoc(docId) {
  if (docId === state.activeDocId) return;
  state.activeDocId = docId;
  // Update the document list highlight in place (keeps the tab row + focus stable).
  mountEl.querySelectorAll(".doc-pill").forEach((pill) => {
    const active = pill.dataset.docId === docId;
    pill.classList.toggle("is-active", active);
    pill.setAttribute("aria-current", active ? "true" : "false");
  });
  loadActiveDoc();
}

async function loadActiveDoc() {
  const tab = tabById(state.activeTabId);
  const doc = tab.docs.find((d) => d.docId === state.activeDocId);
  const seq = ++loadSeq;
  viewportEl.replaceChildren(notice("Loading…"));

  let text;
  try {
    text = await api.getDocumentRaw(tab.tabId, doc.docId);
  } catch (err) {
    if (seq === loadSeq) {
      viewportEl.replaceChildren(notice(`Could not load ${doc.title}: ${err.message || err}`));
    }
    return;
  }
  if (seq !== loadSeq) return; // a newer selection superseded this fetch

  try {
    presenter.render(viewportEl, ADAPTERS[doc.format](text));
  } catch (err) {
    viewportEl.replaceChildren(notice(`Could not render ${doc.title}: ${err.message || err}`));
  }
}
