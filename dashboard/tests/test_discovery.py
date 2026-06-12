"""Unit tests for the Documentation page discovery layer (dashboard/discovery.py).

scan_publish() turns a folder tree into a registry: immediate subfolders are tabs,
files directly inside them are documents. These tests pin the load-bearing details:
deterministic id assignment (a wrong/unstable id silently serves the wrong file),
prefix-retaining tab ids distinct from prettified labels, format/mode mapping, and
the one-level scan scope.
"""

import os

from dashboard.discovery import FORMAT_MODE, scan_publish


def _write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def _tab(tabs, tab_id):
    return next(t for t in tabs if t["tabId"] == tab_id)


def test_subfolders_are_tabs_and_inner_files_are_docs(tmp_path):
    _write(str(tmp_path / "1-guides" / "intro.md"), "# Intro")
    tabs = scan_publish(str(tmp_path))
    assert [t["tabId"] for t in tabs] == ["1-guides"]
    docs = tabs[0]["docs"]
    assert [d["docId"] for d in docs] == ["intro"]
    assert docs[0]["format"] == "md"
    assert docs[0]["relPath"] in ("1-guides/intro.md", os.path.join("1-guides", "intro.md"))


def test_integer_prefix_orders_numerically_not_lexically(tmp_path):
    _write(str(tmp_path / "2-bios" / "a.md"))
    _write(str(tmp_path / "10-archive" / "a.md"))
    _write(str(tmp_path / "1-guides" / "a.md"))
    tabs = scan_publish(str(tmp_path))
    # 1, 2, 10 — not "1", "10", "2"
    assert [t["tabId"] for t in tabs] == ["1-guides", "2-bios", "10-archive"]
    assert [t["order"] for t in tabs] == [1, 2, 10]


def test_label_strips_prefix_and_prettifies(tmp_path):
    _write(str(tmp_path / "3-Client Presentations" / "a.md"))
    _write(str(tmp_path / "1-guides" / "a.md"))
    tabs = scan_publish(str(tmp_path))
    labels = {t["tabId"]: t["tabLabel"] for t in tabs}
    assert labels["3-client-presentations"] == "Client Presentations"
    assert labels["1-guides"] == "Guides"


def test_tabid_retains_prefix_and_is_distinct_from_label(tmp_path):
    _write(str(tmp_path / "1-guides" / "a.md"))
    tab = scan_publish(str(tmp_path))[0]
    assert tab["tabId"] == "1-guides"      # prefix RETAINED in the id
    assert tab["tabLabel"] == "Guides"     # prefix STRIPPED in the label


def test_prefix_only_difference_does_not_collide(tmp_path):
    # Both prettify to the same label "Guides" but must keep distinct ids because
    # the id retains the numeric prefix.
    _write(str(tmp_path / "1-guides" / "a.md"))
    _write(str(tmp_path / "2-guides" / "a.md"))
    tabs = scan_publish(str(tmp_path))
    ids = sorted(t["tabId"] for t in tabs)
    assert ids == ["1-guides", "2-guides"]
    assert len(ids) == len(set(ids))


def test_uppercase_extension_is_normalized(tmp_path):
    _write(str(tmp_path / "1-guides" / "Guide.MD"), "# G")
    doc = scan_publish(str(tmp_path))[0]["docs"][0]
    assert doc["format"] == "md"
    assert doc["mode"] == "reading"


def test_docid_deduped_within_tab_on_slug_collision(tmp_path):
    _write(str(tmp_path / "1-guides" / "My Guide.md"))
    _write(str(tmp_path / "1-guides" / "my-guide.md"))
    docs = scan_publish(str(tmp_path))[0]["docs"]
    ids = [d["docId"] for d in docs]
    assert sorted(ids) == ["my-guide", "my-guide-2"]
    assert len(ids) == len(set(ids))


def test_ids_are_stable_across_scans_including_collisions(tmp_path):
    _write(str(tmp_path / "1-guides" / "My Guide.md"))
    _write(str(tmp_path / "1-guides" / "my-guide.md"))
    first = {d["docId"]: d["relPath"] for d in scan_publish(str(tmp_path))[0]["docs"]}
    second = {d["docId"]: d["relPath"] for d in scan_publish(str(tmp_path))[0]["docs"]}
    # Same id set AND the same id maps to the same physical file every scan.
    assert first == second
    # The clean (un-suffixed) id is assigned deterministically, not by FS order.
    assert "my-guide" in first


def test_empty_and_unsupported_only_subfolders_are_omitted(tmp_path):
    _write(str(tmp_path / "1-guides" / "intro.md"))   # supported
    os.makedirs(str(tmp_path / "2-empty"))             # no files
    _write(str(tmp_path / "3-notes" / "readme.txt"))   # unsupported format only
    tabs = scan_publish(str(tmp_path))
    assert [t["tabId"] for t in tabs] == ["1-guides"]


def test_root_files_and_deeper_nesting_are_ignored(tmp_path):
    _write(str(tmp_path / "loose.md"), "# root file, not a doc")
    _write(str(tmp_path / "1-guides" / "intro.md"))
    _write(str(tmp_path / "1-guides" / "nested" / "deep.md"), "# too deep")
    docs = scan_publish(str(tmp_path))[0]["docs"]
    assert [d["docId"] for d in docs] == ["intro"]


def test_docx_and_pdf_listed_as_known_native_formats(tmp_path):
    _write(str(tmp_path / "2-bios" / "person.docx"))
    _write(str(tmp_path / "2-bios" / "deck.pdf"))
    docs = scan_publish(str(tmp_path))[0]["docs"]
    by_fmt = {d["format"]: d["mode"] for d in docs}
    assert by_fmt == {"docx": "native", "pdf": "native"}
    assert FORMAT_MODE["docx"] == "native" and FORMAT_MODE["pdf"] == "native"


def test_missing_root_returns_empty(tmp_path):
    assert scan_publish(str(tmp_path / "does-not-exist")) == []
