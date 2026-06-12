// Per-format document adapters, keyed by extension. This is the ONLY format-aware
// code in the client. Each turns a raw document into one of two normalized shapes the
// presenter understands:
//   reading: { bodyHtml, toc, mode: "reading" } - a sanitized HTML string to inject.
//   native:  { mount, toc, mode: "native" }     - a mount(bodyEl, styleEl) closure the
//                                                  presenter calls to render in place.
// markdown + html are reading mode; docx is native (docx-preview). pdf drops in later.
//
// Both reading adapters share a single strict sanitize and a single heading -> id +
// TOC pass, so a document's own palette or scripts can never leak into the dashboard
// (the html fixture deliberately carries inline styles to prove this). The native
// trust boundary is different and documented on DOCX_OPTIONS below.

/* global marked, DOMPurify, docx */

// FORBID the style/class attributes and the <style>/<script> tags: DOMPurify keeps
// these by default, which would let a document's warm palette override the Glass
// theme. RETURN_DOM_FRAGMENT hands back a tree we can assign heading ids on.
const STRICT_SANITIZE = {
  FORBID_ATTR: ["style", "class"],
  FORBID_TAGS: ["style", "script"],
  RETURN_DOM_FRAGMENT: true,
};

// docx-preview renderAsync options. className is a UNIQUE prefix (not the library
// default "docx") so every injected document style class is namespaced `.di-docx-*`
// and cannot match a dashboard element: the native analog of the reading-mode leak
// guard. The page-metaphor flags render faithful stacked Word pages (real width,
// margins, page breaks) rather than a reflowed block; header/footer/footnote/endnote
// rendering stays on for fidelity. STRICT_SANITIZE is deliberately NOT applied here:
// docx-preview builds the DOM directly from first-party repo bytes client-side (no
// untrusted string injection), and stripping style/class would destroy the very
// styling that makes native fidelity work.
const DOCX_OPTIONS = {
  className: "di-docx",
  inWrapper: true,
  breakPages: true,
  ignoreWidth: false,
  ignoreHeight: false,
  ignoreLastRenderedPageBreak: true,
  renderHeaders: true,
  renderFooters: true,
  renderFootnotes: true,
  renderEndnotes: true,
};

// GitHub-style slug: lowercase, runs of non-alphanumerics to single hyphens, trimmed.
function slugify(text) {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

// Sanitize raw HTML, give every heading a unique slug id, and collect a flat TOC.
// Returns the normalized reading shape. bodyHtml is sanitized-by-construction.
function buildReading(rawHtml) {
  const fragment = DOMPurify.sanitize(rawHtml, STRICT_SANITIZE);
  const toc = [];
  const used = new Map();
  fragment.querySelectorAll("h1, h2, h3, h4, h5, h6").forEach((heading) => {
    const text = heading.textContent.trim();
    if (!text) return;
    let id = slugify(text) || "section";
    const seen = used.get(id) || 0;
    used.set(id, seen + 1);
    if (seen) id = `${id}-${seen + 1}`;
    heading.id = id;
    toc.push({ level: Number(heading.tagName[1]), text, id });
  });
  const holder = document.createElement("div");
  holder.appendChild(fragment);
  return { bodyHtml: holder.innerHTML, toc, mode: "reading" };
}

// Extension -> adapter. Tab visibility and the document list key off this map, so a
// later phase adding `docx`/`pdf` here surfaces those tabs/docs with no other change.
export const ADAPTERS = {
  md: (text) => buildReading(marked.parse(text)),
  html: (text) => buildReading(text),
  // Native mode: docx-preview renders into elements rather than producing a string,
  // so the docx adapter receives the document BYTES (an ArrayBuffer) and returns a
  // `mount(bodyEl, styleEl)` closure instead of bodyHtml. The presenter calls mount;
  // the closure owns the renderAsync call, keeping docx-preview out of the presenter.
  // No generated TOC (Word TOC fields are unsupported by the library), so toc is [].
  docx: (buffer) => ({
    toc: [],
    mode: "native",
    mount: (bodyEl, styleEl) => docx.renderAsync(buffer, bodyEl, styleEl, DOCX_OPTIONS),
  }),
};
