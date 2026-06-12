"""API tests for the Documentation page endpoints.

GET /api/documentation returns the discovered registry; GET /api/documentation/raw
serves a document's bytes, but ONLY for an entry in the freshly scanned registry and
ONLY from inside the publish root (path traversal and symlink escape are rejected).
The publish root is injected via the get_publish_root dependency override, exactly as
the DB path is in the other suites.
"""

import os

import pytest


def _write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


@pytest.fixture
def publish_tree(tmp_path):
    """A small publish tree: a Guides tab with a markdown + html doc, a Bios tab with
    a pdf (native, no Phase 1 adapter), plus a root-level file that must be ignored."""
    _write(str(tmp_path / "1-guides" / "intro.md"), "# Intro\n\nHello.")
    _write(str(tmp_path / "1-guides" / "welcome.html"), "<h2>Welcome</h2>")
    _write(str(tmp_path / "2-bios" / "person.pdf"), "%PDF-1.4 fake")
    _write(str(tmp_path / "loose.md"), "# not a document")
    return tmp_path


@pytest.fixture
def doc_client(publish_tree):
    from fastapi.testclient import TestClient

    from dashboard.app import app, get_publish_root

    app.dependency_overrides[get_publish_root] = lambda: str(publish_tree)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_registry_shape_and_order(doc_client):
    body = doc_client.get("/api/documentation").json()
    tabs = body["tabs"]
    assert [t["tabId"] for t in tabs] == ["1-guides", "2-bios"]
    assert [t["tabLabel"] for t in tabs] == ["Guides", "Bios"]
    guides = tabs[0]["docs"]
    assert [d["docId"] for d in guides] == ["intro", "welcome"]
    assert {d["format"] for d in guides} == {"md", "html"}


def test_registry_omits_relpath(doc_client):
    # relPath is an internal serving detail; it must not leak to the client.
    tabs = doc_client.get("/api/documentation").json()["tabs"]
    for t in tabs:
        for d in t["docs"]:
            assert "relPath" not in d


def test_raw_serves_document_text(doc_client):
    resp = doc_client.get("/api/documentation/raw", params={"tab": "1-guides", "doc": "intro"})
    assert resp.status_code == 200
    assert "# Intro" in resp.text


def test_raw_unknown_tab_or_doc_is_404(doc_client):
    assert doc_client.get(
        "/api/documentation/raw", params={"tab": "1-guides", "doc": "nope"}
    ).status_code == 404
    assert doc_client.get(
        "/api/documentation/raw", params={"tab": "no-tab", "doc": "intro"}
    ).status_code == 404


def test_raw_rejects_traversal_doc_param(doc_client):
    # A crafted doc id is never in the registry whitelist, so it 404s rather than
    # resolving a path.
    resp = doc_client.get(
        "/api/documentation/raw",
        params={"tab": "1-guides", "doc": "../../../../etc/passwd"},
    )
    assert resp.status_code == 404


def test_raw_rejects_symlink_escape(tmp_path):
    from fastapi.testclient import TestClient

    from dashboard.app import app, get_publish_root

    # A secret outside the publish root, reached via a symlink that looks like a
    # normal .md document inside a tab. Discovery follows the symlink (is_file), but
    # the serve endpoint must reject it because its real path escapes the root.
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    root = tmp_path / "publish"
    os.makedirs(str(root / "1-guides"))
    os.symlink(str(secret), str(root / "1-guides" / "leak.md"))

    app.dependency_overrides[get_publish_root] = lambda: str(root)
    try:
        client = TestClient(app)
        resp = client.get(
            "/api/documentation/raw", params={"tab": "1-guides", "doc": "leak"}
        )
        assert resp.status_code in (403, 404)
        assert "TOP SECRET" not in resp.text
    finally:
        app.dependency_overrides.clear()


def test_unknown_api_route_still_404(doc_client):
    # The catch-all SPA route must not shadow an unknown /api path with index.html.
    resp = doc_client.get("/api/does-not-exist")
    assert resp.status_code == 404
    assert "<!doctype html" not in resp.text.lower()
