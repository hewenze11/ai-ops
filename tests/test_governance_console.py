"""Static hosting for the governance console.

The interesting cases here are not "does it serve bytes" -- they are the ways a
stale cache turns into a support ticket that reads "the approve button does
nothing". Those are what these tests pin down.
"""
import importlib
import sys

import pytest
from fastapi.testclient import TestClient

from ai_ops import governance_console


@pytest.fixture()
def bundle(tmp_path, monkeypatch):
    """Point the installer at a throwaway bundle and rebuild the app route."""
    import ai_ops.app as app_module

    root = tmp_path / "governance"
    root.mkdir()
    (root / "index.html").write_text(
        '<!doctype html><link href="/governance/assets/__AI_OPS_UI_REVISION__/app.css">'
        '<script src="/governance/assets/__AI_OPS_UI_REVISION__/app.js"></script>',
        encoding="utf-8")
    (root / "app.css").write_text("body{color:#111}", encoding="utf-8")
    (root / "app.js").write_text("console.log(1)", encoding="utf-8")

    monkeypatch.setattr(governance_console, "governance_dir", lambda: root)
    return root


@pytest.fixture()
def client(tmp_path, monkeypatch, bundle):
    """A bare app with only the governance routes installed."""
    from fastapi import FastAPI

    app = FastAPI()
    state = governance_console.install_governance(app)
    c = TestClient(app)
    c.governance_state = state
    return c


def test_index_serves_the_bundle_with_a_concrete_revision(client):
    body = client.get("/governance").text
    revision = governance_console.governance_revision()

    assert "__AI_OPS_UI_REVISION__" not in body
    assert f"/governance/assets/{revision}/app.js" in body
    assert f"/governance/assets/{revision}/app.css" in body


def test_the_entry_point_is_never_cached(client):
    for path in ("/governance", "/governance/"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        # Computed per request, so it equals whatever is on disk right now.
        assert response.headers["x-ai-ops-ui-revision"] == governance_console.governance_revision()


def test_a_rebuilt_bundle_is_served_without_a_restart(client, bundle):
    """The failure this prevents: a deploy that changes nothing.

    The revision used to be hashed once at install time. Swapping the bundle
    under a running process then left the worker pinned to the old hash -- the
    page kept requesting bytes that no longer matched what was on disk, so the
    change silently did not take effect. It reads as 'my edit was ignored' in
    development and as 'the rollback did not happen' in production, and both
    send people looking in the wrong place.
    """
    before = client.get("/governance").headers["x-ai-ops-ui-revision"]
    (bundle / "app.js").write_text("console.log('rebuilt')", encoding="utf-8")

    after = client.get("/governance").headers["x-ai-ops-ui-revision"]

    assert after != before
    fresh = client.get(f"/governance/assets/{after}/app.js")
    assert fresh.status_code == 200 and "rebuilt" in fresh.text
    # And the superseded hash is gone, not silently served.
    assert client.get(f"/governance/assets/{before}/app.js").status_code == 404


def test_assets_under_the_current_revision_are_immutable(client):
    revision = governance_console.governance_revision()
    response = client.get(f"/governance/assets/{revision}/app.js")

    assert response.status_code == 200
    assert response.text == "console.log(1)"
    assert "immutable" in response.headers["cache-control"]


def test_a_stale_shell_gets_404_not_last_weeks_bytes(client):
    """The whole point of the revision segment.

    Without this, a page left open across a deploy would keep loading an old JS
    bundle from the browser cache and talk to a new API -- presenting as
    "clicking approve does nothing" rather than as a version problem.
    """
    response = client.get("/governance/assets/deadbeefdeadbeef/app.js")

    assert response.status_code == 404
    assert "reload" in response.json()["detail"].lower()


def test_only_the_two_named_assets_are_reachable(client, bundle):
    (bundle / "secrets.txt").write_text("nope", encoding="utf-8")
    revision = governance_console.governance_revision()

    assert client.get(f"/governance/assets/{revision}/secrets.txt").status_code == 404
    assert client.get(f"/governance/assets/{revision}/index.html").status_code == 404


def test_a_missing_bundle_says_so_instead_of_crashing(tmp_path, monkeypatch):
    """A deployment that forgot the console build should say 'not installed'.

    A 500 or a traceback here reads as a platform bug; the actionable fact is
    that the packaged files are absent.
    """
    from fastapi import FastAPI

    monkeypatch.setattr(governance_console, "governance_dir", lambda: tmp_path / "nowhere")
    app = FastAPI()
    governance_console.install_governance(app)

    response = TestClient(app).get("/governance")

    assert response.status_code == 503
    assert "not installed" in response.json()["detail"]


def test_revision_tracks_content_not_a_hand_bumped_number(bundle):
    before = governance_console.governance_revision()
    (bundle / "app.js").write_text("console.log(2)", encoding="utf-8")

    assert governance_console.governance_revision() != before


def test_a_single_file_bundle_still_hashes(tmp_path):
    """Half-shipped development bundles must hash, not raise at startup."""
    root = tmp_path / "only-html"
    root.mkdir()
    (root / "index.html").write_text("<p>hi</p>", encoding="utf-8")

    revision = governance_console._revision(root)

    assert len(revision) == 16


def test_governance_and_legacy_console_hash_separately():
    """Two pages, two revisions.

    If they shared a hash, editing the governance console would also invalidate
    the workbench cache, which looks like a deploy bug every time it happens.
    """
    from ai_ops import console as legacy

    assert legacy.ASSETS.keys() == {"index.html", "app.css", "app.js"}
    assert governance_console.HASHED == ("index.html", "app.css", "app.js")
    # Same filenames, different directories: the hashes must come from
    # different roots, so the modules must not share a directory helper.
    assert governance_console.governance_dir().name == "governance"
    assert governance_console.governance_dir() != legacy.console_dir()


def test_the_governance_mount_is_registered_on_the_real_app(tmp_path, bundle):
    """Guards the wiring in app.py, not just the router in isolation.

    The failure this catches is real and silent: the module works, its own tests
    are green, and nobody notices the route was never installed on the app that
    actually runs. So build the real app and ask it.
    """
    from ai_ops.app import create_app

    client = TestClient(create_app(str(tmp_path / "control.db"), "governance-token-" + "x" * 24))

    response = client.get("/governance")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    # Served without the admin credential on purpose: the page is inert until a
    # token is typed in, and gating it would only lock out the operator.
    assert client.get("/healthz").status_code == 200
