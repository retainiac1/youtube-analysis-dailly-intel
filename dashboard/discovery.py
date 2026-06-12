"""Filesystem discovery for the Documentation page.

Pure and stdlib-only (os / re). Scans one root folder into a registry: each
IMMEDIATE subfolder is a tab, each file directly inside it is a document. Files at
the root and anything more deeply nested are ignored (one level). Nothing is parsed
here; the server only learns names, formats, and relative paths. This keeps the
dashboard app's "imports only db.py + config.py + stdlib" invariant intact.

Ids are stable lookup keys, derived from name-sorted entries so repeated scans of an
unchanged tree yield identical ids (the serve endpoint validates on those ids, so an
unstable id would silently serve the wrong file). They are kept separate from the
human-facing labels/titles.
"""

import os
import re

# The closed set of document formats and the reading mode each maps to. This is the
# format universe (the adapter seam), NOT content: tab/document names, counts, and
# order are all still discovered. A file whose lowercased extension is not a key here
# is not a document.
FORMAT_MODE = {
    "md": "reading",
    "html": "reading",
    "docx": "native",
    "pdf": "native",
}

# A leading integer followed by a separator is an ordering prefix: "01-Guides",
# "3-Client Presentations". Without a separator the name has no prefix (so "3dprint"
# keeps its leading digit).
_PREFIX_RE = re.compile(r"^(\d+)[-_.\s]+(.*)$")


def _split_prefix(name):
    """(order, rest): integer order and the remaining name, or (None, name)."""
    m = _PREFIX_RE.match(name)
    if not m:
        return None, name
    return int(m.group(1)), m.group(2)


def _slugify(text):
    """URL-safe, lowercase, hyphen-separated id from arbitrary text. Empty input
    (a name that is all punctuation) degrades to a stable placeholder."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    return slug or "item"


def _prettify(text):
    """Title-ish display string: separators to spaces, capitalize each word's first
    letter while preserving the rest (so an all-caps token like API survives)."""
    words = [w for w in re.split(r"[-_\s]+", text) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)


def _dedupe(base, used):
    """Return a unique key for `base`, suffixing -2, -3, ... on repeats. `used` is a
    per-scope dict mutated across calls. Deterministic given a fixed call order."""
    n = used.get(base, 0)
    used[base] = n + 1
    return base if n == 0 else f"{base}-{n + 1}"


def _format_of(filename):
    """Lowercased extension without the dot, or '' if there is none."""
    _, ext = os.path.splitext(filename)
    return ext[1:].lower() if ext else ""


def scan_publish(root):
    """Scan `root` into a list of tabs in display order:

        [{ tabId, tabLabel, order, docs: [{ docId, title, format, mode, relPath }] }]

    Tabs with zero supported documents are omitted (no dead tabs). Display order is
    numeric prefix first (ascending), then unnumbered folders alphabetically; ids are
    assigned in a separate name-sorted pass so they are stable across scans.
    """
    if not os.path.isdir(root):
        return []

    subdirs = sorted(
        (e for e in os.scandir(root) if e.is_dir()),
        key=lambda e: e.name.casefold(),
    )

    used_tab_ids = {}
    tabs = []
    for sub in subdirs:
        order, rest = _split_prefix(sub.name)
        tab_id = _dedupe(_slugify(sub.name), used_tab_ids)

        files = sorted(
            (f for f in os.scandir(sub.path) if f.is_file()),
            key=lambda f: f.name.casefold(),
        )
        used_doc_ids = {}
        docs = []
        for f in files:
            fmt = _format_of(f.name)
            if fmt not in FORMAT_MODE:
                continue
            stem = os.path.splitext(f.name)[0]
            docs.append({
                "docId": _dedupe(_slugify(stem), used_doc_ids),
                "title": _prettify(stem),
                "format": fmt,
                "mode": FORMAT_MODE[fmt],
                "relPath": os.path.relpath(f.path, root),
            })

        if not docs:
            continue

        tabs.append({
            "tabId": tab_id,
            "tabLabel": _prettify(rest),
            "order": order,
            "docs": docs,
        })

    tabs.sort(key=lambda t: (t["order"] is None, t["order"] or 0, t["tabLabel"].casefold()))
    return tabs
