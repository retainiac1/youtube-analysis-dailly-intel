// Generic presenter over the normalized adapter shape { bodyHtml, toc, mode }. It
// never branches on format, only on mode: `reading` is the Glass reading view (a
// sticky table-of-contents beside the article, modeled on the structure of
// examples/hey_habit_tracker_user_guide.html); `native` (docx/pdf in later phases)
// renders the source as-is with no generated TOC.

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

// Build the sidebar nav from the collected headings. A document with no headings
// degrades to no TOC (the article still reads); levels drive indentation in CSS.
function buildToc(toc) {
  const nav = el("nav", { class: "doc-toc-nav", "aria-label": "On this page" });
  if (!toc.length) return nav;
  nav.append(el("p", { class: "doc-toc-label" }, "On this page"));
  for (const { level, text, id } of toc) {
    nav.append(el("a", { class: `doc-toc-link doc-toc-l${level}`, href: `#${id}` }, text));
  }
  // Own the scroll. Inside this History-API SPA the native in-page anchor scroll does
  // not fire: clicking a TOC link updates the hash but the document does not move
  // (verified). Resolve the heading by id (getElementById handles the digit-leading
  // slug ids a CSS selector could not) and scroll it in; scrollIntoView honors the
  // headings' scroll-margin-top, so the section clears the sticky header. The hash is
  // updated for deep-linking without a second jump.
  nav.addEventListener("click", (e) => {
    const link = e.target.closest(".doc-toc-link");
    if (!link) return;
    e.preventDefault();
    const id = decodeURIComponent(link.getAttribute("href").slice(1));
    const target = document.getElementById(id);
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "start" });
    history.replaceState(null, "", `#${id}`);
  });
  return nav;
}

// Render `shape` into `target`, replacing its contents. The presenter branches only on
// mode, never on format. Returns whatever the mode's render produces: the native mount
// returns a Promise (renderAsync) the caller can await to catch parse failures; reading
// returns undefined. The single replaceChildren below is the teardown: each render
// discards the entire prior view (old article AND the native style sink together), so
// switching documents never accumulates injected styles or @font-face rules.
export function render(target, shape) {
  const { toc, mode } = shape;
  const article = el("article", { class: "doc-article" });
  let view;
  let pending;

  if (mode === "native") {
    // docx-preview injects the document's own <style>/@font-face into styleEl. Keeping
    // it inside #documentation (never the shared <head>) scopes those styles to the doc
    // view; the di-docx className namespace keeps them off dashboard elements. The
    // adapter's mount owns the renderAsync call (presenter stays format-agnostic).
    const styleEl = el("div", { class: "doc-native-styles" });
    pending = shape.mount(article, styleEl);
    view = el("div", { class: "doc-reading doc-reading--native" }, [styleEl, article]);
  } else {
    // bodyHtml arrives already sanitized from the adapter: trusted-by-construction.
    article.innerHTML = shape.bodyHtml;
    view = el("div", { class: "doc-reading" }, [
      el("aside", { class: "doc-toc" }, [buildToc(toc)]),
      article,
    ]);
  }

  target.replaceChildren(view);
  return pending;
}
