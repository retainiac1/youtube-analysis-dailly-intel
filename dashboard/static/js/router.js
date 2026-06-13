// Vanilla History-API client router. No framework, no hash routes, no build.
//
// main.js registers a route table (path -> onEnter handler) and the page-nav
// links; this module owns history (pushState / popstate), intercepting page-nav
// clicks, keyboard roving across the nav, and marking the active link with
// aria-current. It deliberately knows nothing about run/lane state: that lives in
// main.js outside the routed regions, so navigation only swaps which page handler
// runs and the selected run and lane persist across pages. Keep the route set
// small; later phases add routes by extending the table passed to initRouter.

let routes = {}; // path -> onEnter()
let leaves = {}; // path -> onLeave(), fired when navigating away from that route
let links = []; // page-nav anchors
let fallback = "/"; // where "/" and unknown paths resolve
let currentPath = null;

// Normalize an href (absolute or relative) to its pathname so the route table can
// be keyed on clean paths like "/board".
function pathFor(href) {
  return new URL(href, location.origin).pathname;
}

// Resolve a path to a known route, falling back for "/" and anything unmapped.
function resolve(path) {
  return routes[path] ? path : fallback;
}

// Roving tabindex: the focused (or active) link is the only tab stop.
function setTabStop(target) {
  for (const a of links) a.tabIndex = a === target ? 0 : -1;
}

function dispatch(path) {
  const route = resolve(path);
  if (!routes[route]) return;
  // Fire the outgoing route's leave handler before swapping pages, but only on a real
  // route change (re-dispatching the same path, e.g. a refresh, must not tear down).
  // Lets a page release resources that outlive its DOM (the Documentation page's pdf.js
  // worker handles); pages without a leave handler are unaffected.
  if (currentPath && currentPath !== route && leaves[currentPath]) leaves[currentPath]();
  currentPath = route;
  for (const a of links) {
    const active = pathFor(a.getAttribute("href")) === route;
    if (active) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
    a.tabIndex = active ? 0 : -1; // active page's link is the tab stop
  }
  routes[route]();
}

export function navigate(path, { replace = false } = {}) {
  const target = resolve(path);
  if (replace || target !== location.pathname) {
    history[replace ? "replaceState" : "pushState"]({}, "", target);
  }
  dispatch(target);
}

export function current() {
  return currentPath;
}

export function initRouter(config) {
  routes = config.routes;
  leaves = config.leaves || {};
  links = config.links || [];
  fallback = config.fallback || "/";

  links.forEach((a, i) => {
    // Intercept plain left-clicks; let modified / middle / non-primary clicks
    // through so the real href still opens in a new tab / window.
    a.addEventListener("click", (e) => {
      if (e.defaultPrevented || e.button !== 0 ||
          e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      navigate(pathFor(a.getAttribute("href")));
    });
    a.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        // Roving focus across the nav (focus only; the user activates with Enter
        // or Space). Enter fires a native click on the anchor, handled above.
        e.preventDefault();
        const dir = e.key === "ArrowRight" ? 1 : -1;
        const next = links[(i + dir + links.length) % links.length];
        setTabStop(next);
        next.focus();
      } else if (e.key === " ") {
        // Space scrolls the page on a link by default; treat it as activation.
        e.preventDefault();
        navigate(pathFor(a.getAttribute("href")));
      }
    });
  });

  // Back / forward: render the URL's route without pushing a new entry.
  window.addEventListener("popstate", () => dispatch(location.pathname));
}

export function start() {
  const path = resolve(location.pathname);
  // Normalize "/" and unknown landing paths so the URL bar matches the rendered
  // page (and back/forward never lands on a bare "/").
  if (location.pathname !== path) history.replaceState({}, "", path);
  dispatch(path);
}
