an: routed "Documentation" page (filesystem-discovered, multi-format)

Context

The dashboard is a vanilla History-API SPA with four routed pages (Board, Trends,
Interpretation, Models). We are adding a fifth routed page, Documentation, that
turns a folder tree into a browsable docs site with zero per-document code. At
startup and on every visit the server scans one configured root folder. Each
immediate subfolder becomes a page-scoped tab; each supported file inside becomes a
selectable document. Selecting a document renders it with an auto-generated table of
contents in a Glass-Theme reading view modeled on
examples/hey_habit_tracker_user_guide.html.

The whole point is genericity: the ONLY hardcoded value is the Publish root path.
Tab names, document names, counts, order, and formats are all discovered from the
filesystem. Adding a subfolder adds a tab; adding a file adds a document. No code
change either way.

This is a read-only feature. It performs NO database writes, so the WAL backup
and db-safety steps in CLAUDE.md do not apply here (stated again under Invariants).

Confirmed decisions (from review Q&A)

- Publish root: docs/publish/ (resolved against the repo root, like DB_PATH).
- Formats and modes:
  - md, html render in reading mode (re-themed Glass reading view, generated
TOC from headings, styled like the example).
  - docx, pdf render in native mode, presented as-is (already formatted).
docx via mammoth-to-HTML (its own structure, no reading-view reconstruction);
pdf via pdf.js page render. TOC only from embedded structure when present.
  - Honest caveat: there is no native browser docx renderer, so "as-is" for docx
means mammoth's faithful HTML, not a pixel match to Word. pdf native pages are
truly as-is.
- Tab ordering: numeric folder-name prefix, stripped for display
(01-Guides sorts first, shows as "Guides"). Unprefixed folders fall back to
alphabetical after prefixed ones. Label is derived purely from the folder name.
- Sequencing: per-format STOP gates (Phase 1 = md + html, Phase 2 = docx,
Phase 3 = pdf, Phase 4 = polish). Same all-four end state, each format its own
reviewable gate.

Source read (cited)

Routing and shell, confirmed against the code:

- Client router table is path -> onEnter(); initRouter/dispatch/navigate
live in dashboard/static/js/router.js:11-98. Route handlers and the registered
routes object are in dashboard/static/js/main.js:100-146 (enterBoard,
enterTrends, enterInterpretation, enterModels) and the routes table near
main.js:325-333.
- Each handler only toggles .hidden on page regions; run/lane state persists
across pages (router.js:6-9, main.js:96-99).
- Backend static mounts /js /css /vendor at dashboard/app.py:843-845; SPA
catch-all @app.get("/{path:path}") at app.py:848-857, registered AFTER every
/api route (app.py:834) and guarding path.startswith("api").
- API docs are served at docs_url="/api/docs" (app.py:54), so our endpoints must
NOT use /api/docs. Path is resolved against _REPO_ROOT (app.py:20); DB path
override pattern DASHBOARD_DB_PATH in get_db_path (app.py:88-98); startup
schema via lifespan init_db (app.py:41-51).
- Header nav links .page-nav-link[data-route] at index.html:38-47; page regions
#board #trends #interpretation #models at index.html:87-123; the SHARED lane
tablist .lane-row > .lane-tabs > .lane-tab[data-lane] at index.html:61-70,
wired by wireTablist/selectLane (main.js:149-167) updating state.lane.
- The [hidden] override (author display defeats UA [hidden]) at
dashboard.css:393-401 for the four current regions, with the same gotcha noted
at dashboard.css:568-572.
- Roving tabindex on the page nav: only the active page's link is the tab stop.
index.html:39-40 gives nav-board tabindex="0" + aria-current="page"; the
other three links carry tabindex="-1" (index.html:41-46); the router maintains
this on navigation (router.js:28-30, router.js:36-41). A new non-default page
link is therefore born with tabindex="-1" BY DESIGN (it is not a copy-paste
artifact); the router promotes it to the tab stop when Documentation is active.
- The runtime-measured header var --header-h is published by sticky-header.js:8-24
(ResizeObserver, no hardcoded height) and consumed at dashboard.css:18 and
dashboard.css:162; the Trends interpretation rail uses the same sticky offset
(dashboard.css:614-619), the precedent the doc sticky TOC follows.
- Offline-vendor convention: single minified UMD under
dashboard/static/vendor/ (today echarts-5.5.1.min.js), loaded by a <script>
in index.html:127-129 before the ES module; served by the /vendor mount. No
build step, no CDN.
- Glass tokens in dashboard/static/css/tokens.css:4-56 (backgrounds, glass, text,
accents, radii, transitions); fonts --font-display: Outfit,
--font-body: Satoshi at tokens.css:46-47; lane colors and reserved
--accent-red mapped in dashboard.css:28-30.
- Example presentation examples/hey_habit_tracker_user_guide.html: hero, mobile
TOC toggle (lines ~383-408), sticky desktop TOC sidebar (~415-434), sections with
id + scroll-margin, IntersectionObserver active-section highlight (~1305-1331),
reading-progress bar (~1279-1288). We take STRUCTURE and layout from it, never its
cream/orange/teal palette or its fonts.
- Config: externalized tunables in SETTINGS_DEFAULTS (config.py:106-118),
hardcoded path constants like DB_PATH = "data/database/swipefile.db"
(config.py:166).
- No Publish/ folder exists yet (verified by find -iname publish and the repo
root listing). No markdown rendering or TOC generation exists anywhere today.
interpretation.js deliberately avoids HTML/markdown (textContent only), so this
is the first rendering path and shares nothing with it.

Architecture

Four layers, format-awareness isolated to the adapters.

1. Discovery layer (server, pure os/pathlib)

A function that scans realpath(PUBLISH_ROOT) and returns an in-memory registry:

[{ tab, tabLabel, order, docId, title, relPath, format, mode }]

- Only IMMEDIATE subfolders of Publish are tabs; only files DIRECTLY inside them are
documents. Files at the Publish root are ignored; deeper nesting is ignored
(one level only, stated explicitly).
- format = lowercased file extension; mode is derived (reading for md/html,
native for docx/pdf) via a small extension to mode map. An extension with no
adapter is still listed but flagged unsupported (degrades to a download link,
never crashes the tab).
- order/tabLabel: parse an optional leading numeric prefix and separator from
the folder name. The prefix is parsed as an INTEGER and sorted numerically (so
10-Archive follows 2-Bios, not precedes it). Sort by (integer prefix or sentinel-large, case-insensitive name); label is the folder name with the prefix
stripped and prettified (separators to spaces, title-cased). Pure function of the
folder name.
- docId: slug of the filename (stable, URL-safe), DEDUPED within its tab. Two
files in one folder that slug to the same value (for example My Guide.md and
my-guide.md) would make the ?doc= lookup ambiguous, so a collision gets a
numeric suffix to keep docIds unique per tab. title: prettified filename (the
client may upgrade the displayed title to the document's first heading).
- A subfolder with zero supported documents is omitted (no dead tabs). The moment a
supported file lands there, the next live scan surfaces the tab.
- Re-run cheaply on each registry request (live pickup, no restart). This registry
REPLACES any hand-maintained doc list; nothing is enumerated in code.

This stays within the server invariant: it imports only stdlib (os, pathlib),
plus config. It only scans the tree and (separately) serves bytes. It never parses
a document.

2. Normalized internal shape (client)

Every adapter produces the same shape:

{ bodyHtml, toc: [{ level, text, id }], mode: "reading" | "native" }

3. Per-format adapters (client, keyed by extension)

The ONLY format-aware code. Each is a pure bytes/text -> normalized shape:

- markdown (Phase 1): vendored marked (single minified UMD, no deps, no
build). A heading renderer override emits GitHub-style slug ids (lowercase,
spaces to hyphens, strip non-word, dedupe suffix), and the TOC is collected from
the headings during render. Rendered HTML is sanitized (vendored DOMPurify)
before injection, defense in depth even though docs are first-party. mode: "reading".
- html (Phase 1): parse with DOMParser, lift the <body> content, slug
headings, collect TOC. mode: "reading". Theme governance is NOT left to default
sanitize: default DOMPurify keeps style attributes, in-body <style> blocks, and
class, so a document's own palette would survive and leak cream/orange/teal into
the dashboard (the one thing forbidden). The html adapter sanitizes with
FORBID_ATTR: ["style", "class"] and FORBID_TAGS: ["style", "script"], AND
injects the result into a scoped reading container whose Glass tokens govern
appearance. Both layers, not one. The html adapter is fragment-oriented: it renders
document BODY CONTENT, not a standalone page (no foreign hero, sidebar, or scripts).
- docx (Phase 2): vendored mammoth.browser converts bytes to HTML
client-side; shown as-is in native mode (its own structure, minimal re-theming),
TOC from h1..h3 when present.
- pdf (Phase 3): vendored pdf.js (pdf.min.js + pdf.worker.min.js; note:
heavy, on the order of a megabyte) renders pages to canvas in native mode; TOC
from the embedded outline (getOutline), none if the PDF is flat or scanned.
- An extension with no adapter: registry-flagged unsupported, presenter shows a
download link, no crash.

All parsing is client-side, so no docx/pdf parsing dependency ever enters the server
path (invariant preserved). docx/pdf bytes are fetched and parsed in the browser.

4. Generic presenter (client)

One presenter over the normalized shape. It never branches on format, only on
mode:

- reading: Glass reading view modeled on the example structure (type hierarchy,
readable line measure, sticky left TOC, active-section nav, anchor deep-links).
Re-themeable via tokens; light/dark follows the existing theme toggle.
- native: source rendered as-is (docx HTML block, or pdf canvas pages), TOC only
from the embedded outline/headings.

Layout, scroll offsets, and the TOC sidebar width come from CSS tokens / a runtime
measured header var (--header-h via sticky-header.js), never hardcoded rem
values (no magic numbers).

Files

New (client):

- dashboard/static/js/documentation.js: page controller. Fetches the registry,
renders the page-scoped tab bar and per-tab document list, fetches a selected
document's bytes, dispatches to the right adapter, hands the normalized shape to
the presenter. Owns #documentation via replaceChildren (the Models precedent).
- dashboard/static/js/doc-adapters.js: extension-keyed adapter registry
(markdown + html in Phase 1; docx, pdf added later as pure additions).
- dashboard/static/js/doc-presenter.js: the generic presenter (reading + native).
- dashboard/static/vendor/marked-<version>.min.js,
dashboard/static/vendor/dompurify-<version>.min.js (Phase 1); mammoth (Phase 2);
pdf.js core + worker (Phase 3). All vendored offline, pinned, no CDN.

Modified:

- config.py (near line 166): add PUBLISH_ROOT = "docs/publish". Single hardcoded
value. Optionally honor a DASHBOARD_PUBLISH_PATH env override mirroring
DASHBOARD_DB_PATH so a smoke can point at a throwaway tree.
- dashboard/app.py: add a path-resolution helper (mirrors get_db_path,
app.py:88-98) that resolves PUBLISH_ROOT against _REPO_ROOT; add two API
routes BEFORE the static mounts and catch-all (so they keep their specificity):
  - GET /api/documentation returns the freshly scanned registry JSON.
  - GET /api/documentation/raw?tab=&doc= validates (tab, doc) against the
freshly scanned registry (a dynamic whitelist), resolves the stored path, and
returns the bytes via FileResponse. Not /api/docs (reserved by docs_url).
- dashboard/static/index.html: add <a class="page-nav-link" data-route href="/documentation" id="nav-documentation" tabindex="-1">Documentation</a> to
the page nav (index.html:38-47); add <main class="documentation" id="documentation" aria-live="polite" hidden></main> alongside the other regions
(index.html:87-123); add the vendored <script> tags near the ECharts one
(index.html:127-129).
- dashboard/static/js/main.js: add enterDocumentation() (show #documentation,
hide the others, hide rail/drawer like Models since the page is not run/lane
scoped) and register "/documentation": enterDocumentation in the routes table.
- dashboard/static/css/dashboard.css: add .documentation[hidden] { display: none; } to the override block (dashboard.css:393-401) because the page region
uses an author display; add the page-scoped tab bar, document list, reading
view, and sticky TOC styles using Glass tokens only.

Resolved design decisions

1. Discovery timing: scan at startup AND on each visit (live pickup). The scan
is a cheap directory walk; live pickup means a new file appears with no restart.
2. Tab ordering: numeric prefix, stripped for display (see Confirmed decisions).
3. Tab labels: derived from the folder name only (strip numeric prefix,
separators to spaces, title-case). No hardcoded names.
4. Empty / unsupported-only subfolders: omitted (no dead tabs). A tab appears as
soon as it holds at least one supported document.
5. Scan scope: immediate subfolders are tabs; files directly inside them are
documents. Root-level files and deeper nesting are ignored. One level, for now.
6. Markdown library: vendored marked (single UMD, no build) with a heading
renderer that emits GitHub-style slug ids, matching the ECharts offline
convention. Sanitized with vendored DOMPurify.
7. Future adapters: mammoth.browser (docx to HTML, native mode) and pdf.js
(native pages + getOutline TOC, note the megabyte-scale weight). Both vendor
with no build step. Some formats (for example pptx) have no clean in-browser
reading view and would degrade to download or native; not implemented now, the
adapter seam makes them pure additions.
8. TOC quality / degradation: headings drive the TOC for md/html (and docx
when present); pdf relies on the embedded outline and has none if flat or
scanned; a doc with no headings degrades to no TOC, body still readable.
9. Existing in-file TOC: the presenter always generates its own sidebar TOC and
does NOT try to detect or strip an author "Contents" section (too fragile). Any
in-document contents list is honored as authored, alongside our sidebar TOC.
10. Serve endpoint / path safety: serve ONLY from within realpath(PUBLISH_ROOT).
The requested (tab, doc) is validated against the freshly scanned registry
(dynamic whitelist); the served path is taken from the matched registry entry,
realpath-resolved, and rejected unless it stays inside the Publish realpath
(blocks path traversal and symlink escape). No path is ever built from a raw
user string. No arbitrary filesystem read.
11. Render safety: docs are first-party repo content, but md/html rendered HTML
is still sanitized with DOMPurify before injection (defense in depth). For html,
sanitize also enforces theme governance, not just safety: FORBID_ATTR
(style, class) and FORBID_TAGS (style, script) strip any foreign
palette, and the body is injected into a scoped Glass-token container. docx/pdf
bytes are parsed entirely client-side; pdf renders to canvas (no HTML injection).

Invariants

- The ONLY hardcoded value is PUBLISH_ROOT (one config constant). No tab names, no
document names, no format list baked into the UI, no counts.
- Follow the routed-pages pattern exactly: add /documentation to the router table
and the header nav, rely on the existing catch-all, and add .documentation to
the [hidden] override.
- The page-scoped category tabs are SEPARATE from the global lane tabs. They live
inside #documentation with their own classes (for example .doc-tabs/.doc-tab),
never .lane-tab / [data-lane], never touching state.lane or selectLane.
The global lane row stays visible but unused on this page, matching the Models
precedent.
- Glass Theme tokens only, no new colors or fonts; locked lane colors and reserved
red unused.
- Offline vendoring, no CDN, for every client library.
- The dashboard server imports only db.py and config.py (plus stdlib). All
document PARSING stays client-side; the server only scans the tree and serves
bytes. No docx/pdf parsing dependency enters the server path.
- Read-only feature: NO database writes. The WAL backup and db-safety steps do not
apply. (The startup init_db lifespan is unrelated to this feature and unchanged.)

Phasing (STOP after each)

- Phase 1: routing + Publish discovery layer + page-scoped tabs from subfolders
  - the two serve endpoints + per-tab document list + markdown and html adapters
(reading mode) + the generic presenter and Glass reading view matching the example
structure. Proves discovery, tabs, and the adapter-plus-presenter seam end to end
on real content. Seed docs/publish/ with at least one subfolder containing a
.md document and a simple CONTENT-ONLY .html fragment (a body fragment with
headings, no <head>, no foreign sidebar/hero/scripts). Do NOT seed the example
guide as the html fixture: it is a full standalone page (its own hero, sticky TOC,
IntersectionObserver, reading-progress, scripts), so a fragment adapter plus the
generic presenter would produce nested layouts and a duplicated TOC, masking
whether the adapter or the fixture is at fault. The example stays strictly the
STRUCTURAL model for the presenter (what "take structure from it" meant), never a
rendered test document. STOP for review.

- Phase 2
- PLANNING TASK, Phase 2 (docx via docx-preview). Plan only, no code. Plan before
execute, explicit STOP gate, bring the plan for review before writing anything. No
em dashes. No magic numbers. Code wins over docs: read the source and cite
file:line, do not guess. Verify behavior in a real browser, never by serving a file.

GOAL
Add docx support so Word documents render on the Documentation page in NATIVE mode:
the user picks a .docx from the tab's document list and the document displays
faithfully (looking like the Word document) in a scrollable container, using
docx-preview. No generated sidebar TOC. This is the native branch the presenter
left stubbed in Phase 1.

LIBRARY DECISION IS SETTLED: docx-preview (npm "docx-preview",
github.com/VolodymyrBaydalka/docxjs), chosen over mammoth for fidelity ("present
as-is"). Do not relitigate. Research-confirmed facts to build on, not rediscover:
- renderAsync(document, bodyContainer, styleContainer, options) renders the doc
  directly into bodyContainer and injects document styles/fonts into styleContainer
  (separate element; if null, bodyContainer is used). It does NOT return an HTML
  string. document accepts Blob | ArrayBuffer | Uint8Array.
- It depends on JSZip, loaded BEFORE docx-preview.
- Word TOC fields are NOT supported by the library (confirmed in its README): a
  Word table of contents renders as static text with NO working links. This is
  accepted. We do not build TOC extraction (honors "no TOC extraction"). The native
  shape carries toc: [] and there is no sidebar TOC for docx.

THE CONTRACT CHANGE CC MUST RESOLVE FIRST (this is NOT a pure addition)
The Phase 1 adapters are pure bytes/text -> { bodyHtml, toc, mode } string
producers, and the presenter injects bodyHtml. docx-preview does not produce a
string; it renders into an element and manages its own styles. So the adapter seam
must be extended minimally to support a "render into the presenter's target"
adapter WITHOUT changing the md/html string path. Propose the cleanest extension,
for example a native shape variant that carries a mount(bodyEl, styleEl) function
the presenter calls, versus the presenter's native branch owning the renderAsync
call. Read doc-adapters.js, doc-presenter.js, and documentation.js and cite the
exact lines the change touches. State why the chosen shape keeps md/html untouched.

VENDORING (offline, no CDN, like ECharts and the Phase 1 libs)
- Vendor BOTH jszip and docx-preview as pinned globals under
  dashboard/static/vendor/ (version in the filename), JSZip first, then
  docx-preview, both before the ES module, served by the existing /vendor mount.
- Pin the current published docx-preview version; verify the exact version at
  vendor time rather than assuming a number. Confirm jszip is not already vendored
  before adding a second copy.

ADAPTER + PRESENTER
- docx is BINARY. The Phase 1 raw fetch returns text; the docx path must fetch the
  bytes as an ArrayBuffer. State exactly how the api layer returns bytes for native
  formats vs text for reading formats so the two paths are explicit, not conflated.
- The docx adapter/native branch calls docx.renderAsync(arrayBuffer, bodyEl,
  styleEl, options). Decide and justify the options, in particular:
  - className set to a UNIQUE prefix (not the default "docx") so injected document
    style classes are namespaced and cannot collide with dashboard styles.
  - a dedicated styleContainer element inside #documentation (not the shared head),
    so the document's injected styles/fonts are scoped to the doc view.
  - the page-metaphor options (ignoreWidth, ignoreHeight, breakPages,
    ignoreLastRenderedPageBreak) per the UX decision below.
  - renderHeaders/Footers/Footnotes/Endnotes left at sensible defaults; state them.
- DO NOT apply the reading-mode strict sanitize (FORBID style/class) to docx output.
  That config exists to strip foreign palettes in reading mode; applying it to
  docx-preview would destroy the very styling that makes native fidelity work.
  These are first-party repo docs. State the trust boundary explicitly: docx-preview
  builds the DOM directly from first-party bytes, client-side, no string injection
  of untrusted markup, so the reading-mode sanitize does not apply here. If any
  sanitize is proposed, justify it does not strip docx-preview's style classes.

UX DECISION TO SURFACE (do not silently pick)
docx-preview can render either as stacked Word pages (page width/margins/breaks,
the literal document look) or as a flowing scrollable block (ignoreWidth and page
breaks off). The faithful "page" look means light document surfaces against the
dark Glass chrome, which is normal for a document viewer but is a visual contrast.
Present both, with a lean and the option flags each needs, and let the reviewer
choose. Either way the surrounding chrome (the doc-view container, scroll area)
uses Glass tokens; only the rendered document interior carries the doc's own look.

STILL TRUE FROM PHASE 1 (confirm against code)
- The server already lists docx as a known native format (FORMAT_MODE has
  docx: native) and never parses it. Confirm no server change is needed.
- Tab visibility keys on the client ADAPTERS registry, so registering the docx
  adapter should make the Bios tab and its docs appear with no controller change
  beyond the registration and the native-render seam. Confirm this.

INVARIANTS (carried)
- Only hardcoded value remains PUBLISH_ROOT. Glass tokens for chrome; no new colors
  or fonts added to the theme (the doc's own fonts are the library's concern, scoped
  to the doc view). Page-scoped doc tabs stay separate from the global lane tabs.
- Offline vendoring, no CDN. Server imports only db.py/config.py/stdlib; ALL docx
  parsing and rendering is client-side. Read-only, no DB writes.

VERIFICATION (real browser, Desktop Chrome via playwright-core, do not assume)
Serve against a VACUUM INTO copy DB via DASHBOARD_DB_PATH. Seed a real .docx into a
Bios subfolder (ideally one bio with a field-generated Word TOC and one without).
Confirm:
- The Bios tab appears once the adapter is registered; its docx documents list;
  selecting one renders via docx-preview in a scrollable container.
- Fidelity: headings, lists, tables, images, and headers/footers render; the
  document looks like the Word doc.
- Containment: after a docx renders, the dashboard chrome (page nav, doc tabs,
  Glass tokens, fonts elsewhere) is visually unchanged. Inspect that the injected
  document styles are scoped under the chosen className and do not match dashboard
  elements. This is the native analog of the reading-mode leak guard.
- The Word-TOC reality is demonstrated, not assumed: confirm a Word TOC renders as
  static text and its entries do not navigate (expected, since fields are
  unsupported). No surprise, no extraction attempted.
- Regression: md and html reading-mode docs still render with their generated TOC,
  unaffected by the native path or the seam change.

OUTPUT
A plan only, including the proposed adapter-seam extension with cited line numbers
and the two UX options for the page-vs-flow look. No code. Bring it for review. 
STOP.

- Phase 3 (future): pdf adapter via pdf.js (native mode, outline-based TOC).
STOP.

- Phase 4 (optional): polish (sticky TOC, active-section on scroll via
IntersectionObserver, deep-link anchors, reading-progress bar). STOP.

Use the frontend-design skill for the reading experience and the tab bar within
the Glass Theme during implementation.

Verification (per phase, real browser)

- Unit/route tests: registry scan over a temporary tree (tabs ordered by numeric
prefix, labels prettified, empty subfolders omitted, root files and deeper nesting
ignored, unsupported extensions flagged); serve endpoint rejects traversal and
symlink escape and a (tab, doc) not in the registry; endpoints registered before
the catch-all and not shadowing /api/docs. Run under the existing pytest setup
(dashboard/tests/).
- Manual browser validation (never by serving a file): launch the dashboard against
a COPY-pointed DB via DASHBOARD_DB_PATH and drive the user's real Chrome with
playwright-core. Confirm: the Documentation nav link routes and marks
aria-current; tabs render from the seeded subfolders with no hardcoded names;
selecting a document renders it in the Glass reading view with a working generated
TOC and anchor links; adding a file then revisiting surfaces it with no restart;
an unsupported file degrades to a download link without breaking the tab; the
[hidden] toggle actually hides the region when navigating away; lane tabs are
untouched by the page-scoped tabs.

The plan is approved and saved. Per the task's explicit terms ("Plan before execute, one phase at a time, explicit STOP gate, bring the plan for review before writing anything ... A plan only. No code. STOP."), I'm stopping here rather than rolling into Phase 1. Approval of the plan is not the same as the go-ahead to start writing code, and the plan's own first STOP gate sits before any implementation.

