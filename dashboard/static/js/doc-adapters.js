// Per-format document adapters, keyed by extension. This is the ONLY format-aware
// code in the client. Each turns a raw document into one of two normalized shapes the
// presenter understands:
//   reading: { bodyHtml, toc, mode: "reading" } - a sanitized HTML string to inject.
//   native:  { mount, toc, mode: "native" }     - a mount(bodyEl, styleEl) closure the
//                                                  presenter calls to render in place.
// markdown + html are reading mode; docx and pdf are native (docx-preview / pdf.js).
//
// Both reading adapters share a single strict sanitize and a single heading -> id +
// TOC pass, so a document's own palette or scripts can never leak into the dashboard
// (the html fixture deliberately carries inline styles to prove this). The native
// trust boundary is different and documented on DOCX_OPTIONS / the pdf adapter below.

/* global marked, DOMPurify, docx, pdfjsLib */

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

// pdf.js renders pages on a Web Worker it spawns from this path. If workerSrc is left
// unset pdf.js tries to fetch the worker from a CDN (or fails), so point it at the
// vendored worker to keep the page fully offline. Set once here at module load, before
// any getDocument; the worker file is vendored but deliberately NOT a <script> tag (see
// index.html). The version in the path tracks the vendored filename.
pdfjsLib.GlobalWorkerOptions.workerSrc = "/vendor/pdf.worker-3.11.174.min.js";

// getDocument options shared by every pdf render. isEvalSupported:false disables pdf.js's
// font-program eval path: the mitigation for CVE-2024-4367 (arbitrary JS execution via a
// crafted PDF, fixed upstream in 4.2.67, after this pinned 3.11.174). These bytes are
// first-party repo files so exposure is near zero, but the guard is free defense in depth.
const PDF_DOC_OPTIONS = { isEvalSupported: false };

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
  // pdf.js renders each page to a <canvas> (no HTML string, no injected stylesheet), so
  // like docx the adapter takes BYTES and returns a mount closure - but it also owns
  // teardown state. getDocument returns a loadingTask/PDFDocumentProxy and each page a
  // RenderTask; all must be destroyed/cancelled when the user switches docs or leaves the
  // page, or a pending render leaks a worker handle or paints into a replaced canvas. mount
  // and destroy close over the shared state below; destroy is the optional teardown handle
  // the controller fires on the PREVIOUS render before the next one (md/html/docx omit it).
  //
  // Trust boundary: pdf.js paints first-party repo bytes to canvas client-side with no HTML
  // string injection, so no sanitize applies (safer than docx). isEvalSupported is off too.
  pdf: (buffer) => {
    let loadingTask = null;
    let pdfDoc = null;
    const renderTasks = [];
    let destroyed = false;

    return {
      toc: [], // no sidebar this phase; the outline-driven TOC is a later phase
      mode: "native",
      // styleEl is unused (pdf injects no stylesheet); accept and ignore it so the native
      // mount(bodyEl, styleEl) signature stays identical across formats.
      mount: async (bodyEl) => {
        loadingTask = pdfjsLib.getDocument({ data: buffer, ...PDF_DOC_OPTIONS });
        try {
          pdfDoc = await loadingTask.promise;
        } catch (err) {
          // destroy() aborts the load and rejects this promise: benign teardown, bail
          // quietly. Any other rejection is a real parse fault (corrupt pdf) and must
          // propagate so the controller shows its "Could not render" notice.
          if (destroyed) return;
          throw err;
        }
        // A destroy() can land mid-load: it sets `destroyed` and cancels whatever render
        // tasks exist at that instant, but pages created by LATER awaits would still paint
        // into a replaced canvas. So re-read the flag after EVERY await and bail, leaving
        // nothing running. This post-await re-check (not just an entry check) is what
        // closes the switch-before-render-finishes race.
        if (destroyed) return;
        // Match the device pixel ratio so canvases stay crisp on HiDPI (no magic scale).
        const scale = window.devicePixelRatio || 1;
        for (let n = 1; n <= pdfDoc.numPages; n += 1) {
          const page = await pdfDoc.getPage(n);
          if (destroyed) return;
          const viewport = page.getViewport({ scale });
          const canvas = document.createElement("canvas");
          canvas.className = "doc-pdf-page";
          canvas.width = viewport.width;
          canvas.height = viewport.height;
          // Lay the canvas out at CSS-pixel size; the larger backing store is what gives
          // the crisp HiDPI render. Both derive from the one scaled viewport, so the
          // ratio is exact and there are no hardcoded page dimensions.
          canvas.style.width = `${viewport.width / scale}px`;
          canvas.style.height = `${viewport.height / scale}px`;
          bodyEl.append(canvas);
          const task = page.render({ canvasContext: canvas.getContext("2d"), viewport });
          renderTasks.push(task);
          try {
            await task.promise;
          } catch (err) {
            // destroy() cancels in-flight renders; pdf.js rejects those with
            // RenderingCancelledException. That is expected teardown, not a failure.
            if (destroyed || err?.name === "RenderingCancelledException") return;
            throw err;
          }
          if (destroyed) return;
        }
      },
      destroy: () => {
        if (destroyed) return;
        destroyed = true;
        // Cancel whatever renders exist now; mount's post-await checks stop any not yet
        // created. Then release the worker-side document and loading task.
        for (const task of renderTasks) {
          try { task.cancel(); } catch (e) { /* already settled */ }
        }
        if (loadingTask) {
          try { loadingTask.destroy(); } catch (e) { /* best effort */ }
        }
      },
    };
  },
};
