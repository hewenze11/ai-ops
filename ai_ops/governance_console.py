"""Governance console: static hosting for the permission-control surface.

This is a *separate* page from the legacy workbench at ``/``. The legacy console
is how an operator drives the platform; this one is where the operator answers
the one question the product is actually sold on -- "the AI stopped and is
waiting for my nod; what is it asking for?"

Boundaries, same as ``console.py`` and for the same reasons:

* Assets are served WITHOUT the admin credential. HTML/CSS/JS carry no secret,
  and the page is useless until the operator types a token, which only ever
  travels in the ``Authorization`` header.
* Nothing here adds authority. Every endpoint this page calls already exists
  under ``/api/v1/*`` behind ``admin``; this module only serves bytes.

Why a separate module instead of another route in ``console.py``: the two pages
are replaced on different schedules by different authors, and the revision hash
must only cover the bundle it actually belongs to. Sharing one installer would
mean a governance edit invalidates the workbench cache (and vice versa), which
looks like a deploy bug every single time.
"""
import hashlib
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse

ASSETS = {
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}

# Files the revision hash covers: everything the page can load. Missing files
# are tolerated because the bundle may legitimately ship as a single HTML file
# during development; a half-shipped bundle then still hashes deterministically
# instead of raising at startup.
HASHED = ("index.html", "app.css", "app.js")

MOUNT = "/governance"


def governance_dir():
    return Path(__file__).resolve().parent / "console" / "governance"


def _revision(root):
    digest = hashlib.sha256()
    for name in HASHED:
        path = root / name
        digest.update(name.encode() + b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
        else:
            digest.update(b"missing")
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def install_governance(app):
    root = governance_dir()
    headers = {"Cache-Control": "no-store"}

    def _read(name):
        path = root / name
        if not path.is_file():
            # A clear, actionable 404 beats a stack trace: the usual cause is a
            # deployment that shipped the package without the console build.
            raise HTTPException(503, "Governance console bundle is not installed")
        return path

    @app.get(MOUNT, include_in_schema=False)
    @app.get(MOUNT + "/", include_in_schema=False)
    def index():
        # Computed per request, NOT once at startup. Hashing at import time
        # means a bundle shipped or replaced under a running process is never
        # served -- the revision stays pinned to whatever was on disk when the
        # worker started, and every request keeps pointing at bytes that may no
        # longer match. During development that reads as "my change did not
        # take effect"; during a deploy it reads as a rollback that did not
        # happen. The file set is three files, so the cost is a few hundred KB
        # of reading per page load, which is nothing next to being wrong.
        revision = _revision(root)
        html = _read("index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__AI_OPS_UI_REVISION__", revision), headers={
            **headers, "X-AI-Ops-UI-Revision": revision})

    @app.get(MOUNT + "/assets/{requested_revision}/{filename}", include_in_schema=False)
    def versioned_asset(requested_revision: str, filename: str):
        if filename not in ("app.css", "app.js"):
            raise HTTPException(404, "Not found")
        revision = _revision(root)
        # Immutable for a full year *only* under the hash that produced it. A
        # stale shell asking for last week's bundle gets a 404 telling it to
        # reload, rather than bytes from a different build marked immutable.
        if requested_revision != revision:
            raise HTTPException(404, "UI asset version not found; reload the page")
        return FileResponse(root / filename, media_type=ASSETS[filename],
                            headers={**headers, "X-AI-Ops-UI-Revision": revision,
                                     "Cache-Control": "public, max-age=31536000, immutable"})

    return {"revision": _revision(root), "root": str(root)}


def governance_revision():
    """Current bundle hash, for tests and for the deploy smoke check."""
    return _revision(governance_dir())
