"""The console API doc is the front-end author's only input.

Which makes it a contract, and contracts rot silently: a route gets renamed, the
doc keeps promising the old path, and the front end ships a screen that 404s
only when someone clicks it. This test reads the doc as text and asks the real
app whether every path it promises exists.

It is deliberately not cleverer than that. Parsing JSON schemas out of markdown
would be its own source of drift; the cheap check catches the expensive failure.
"""
import pathlib
import re
import tempfile

import pytest

from ai_ops.app import create_app

DOC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "governance-console-api.md"


@pytest.fixture(scope="module")
def routes(tmp_path_factory):
    path = tmp_path_factory.mktemp("doc") / "control.db"
    app = create_app(str(path), "doc-route-check-" + "y" * 24)
    return {getattr(r, "path", "") for r in app.routes}


def documented_paths():
    return sorted(set(re.findall(r"/api/v1/[A-Za-z0-9/{}\-_]+", DOC.read_text(encoding="utf-8"))))


def test_every_documented_route_exists(routes):
    missing = [p for p in documented_paths() if p not in routes]

    assert not missing, (
        "governance-console-api.md promises routes the app does not serve; the "
        f"front end would 404 on click: {missing}"
    )


def test_the_doc_is_not_empty(routes):
    """A truncated doc would make the test above pass vacuously."""
    assert len(documented_paths()) >= 25


def test_the_inbox_doc_matches_the_real_response_shape(tmp_path):
    """`total` counts four card kinds, not three.

    An off-by-one here is not cosmetic: the sidebar badge would disagree with
    the list every single time a masked read is pending.
    """
    from ai_ops.inbox import inbox

    doc = DOC.read_text(encoding="utf-8")
    app = create_app(str(tmp_path / "control.db"), "inbox-shape-check-" + "z" * 24)
    with app.state.read_transaction() as db:
        body = inbox(db)

    assert set(body) == {"approvals", "questions", "account_requests", "sensitive_hits", "total", "note"}
    for key in ("approvals", "questions", "account_requests", "sensitive_hits"):
        assert f'"{key}"' in doc, f"the doc never mentions the {key} card list"


def test_the_doc_forbids_showing_secret_values():
    """The one rule a well-meaning implementer is most likely to 'fix'.

    Kept as an assertion on the doc because the doc is what the implementer
    reads; the code-level guarantee lives in the credentials tests.
    """
    doc = DOC.read_text(encoding="utf-8")

    assert "不得出现" in doc
    assert "查看明文" in doc
