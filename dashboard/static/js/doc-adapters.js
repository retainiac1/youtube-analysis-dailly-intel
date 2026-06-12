// Per-format document adapters: each turns raw document text into the one normalized
// shape the presenter understands, { bodyHtml, toc, mode }. This is the ONLY
// format-aware code in the client. Phase 1 ships markdown + html (both reading mode);
// docx and pdf drop in later as pure additions keyed by extension.
//
// Both reading adapters share a single strict sanitize and a single heading -> id +
// TOC pass, so a document's own palette or scripts can never leak into the dashboard
// (the html fixture deliberately carries inline styles to prove this).

/* global marked, DOMPurify */

// FORBID the style/class attributes and the <style>/<script> tags: DOMPurify keeps
// these by default, which would let a document's warm palette override the Glass
// theme. RETURN_DOM_FRAGMENT hands back a tree we can assign heading ids on.
const STRICT_SANITIZE = {
  FORBID_ATTR: ["style", "class"],
  FORBID_TAGS: ["style", "script"],
  RETURN_DOM_FRAGMENT: true,
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
};
