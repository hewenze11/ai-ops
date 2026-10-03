"""Console static assets and the small read endpoints it depends on.

These tests pin the two properties that matter most for an admin surface:

* the static assets are served without any credential (they contain no secret),
* every data endpoint the console calls still requires the admin credential and
  rejects an anonymous caller.
"""
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from ai_ops.app import create_app

ADMIN = "console-test-admin-not-real-1234567890"


def make_client():
    return TestClient(create_app(str(Path(tempfile.mkdtemp()) / "control.db"), ADMIN, "test-model"))


def setup(client, headers):
    client.post("/api/v1/roles", headers=headers, json={"id": "ops", "name": "Ops"})
    asset = client.post("/api/v1/assets", headers=headers, json={
        "id": "test-linux", "name": "Test Linux", "allowed_users": ["ops_read", "ops_admin"]})
    return asset.json()


def test_index_and_assets_are_public_and_nonempty():
    client = make_client()
    page = client.get("/")
    assert page.status_code == 200
    assert "AI 运维控制台" in page.text
    assert "text/html" in page.headers["content-type"]

    css = client.get("/assets/app.css")
    assert css.status_code == 200 and ".split" in css.text

    script = client.get("/assets/app.js")
    assert script.status_code == 200
    # No build step, and the script must never embed a credential.
    assert "Authorization" in script.text
    assert ADMIN not in script.text


def test_console_data_endpoints_require_admin():
    client = make_client()
    for path in ("/api/v1/roles", "/api/v1/console/tasks", "/api/v1/console/overview"):
        assert client.get(path).status_code in (401, 403)
    assert client.get("/").status_code == 200


def test_roles_listing_and_console_tasks():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)
    roles = client.get("/api/v1/roles", headers=headers)
    assert roles.status_code == 200
    assert any(r["id"] == "ops" for r in roles.json())

    created = client.post("/api/v1/tasks", headers=headers, json={
        "role_id": "ops", "asset_id": "test-linux", "execution_users": ["ops_read"],
        "run_as": "ops_read", "command": "id -un", "timeout_seconds": 30,
        "mode": "direct", "idempotency_key": "console-task-0001"})
    assert created.status_code == 201

    tasks = client.get("/api/v1/console/tasks?limit=10", headers=headers)
    assert tasks.status_code == 200
    assert any(t["payload"]["command"] == "id -un" for t in tasks.json())


def test_console_overview_counts_and_notices():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)
    data = client.get("/api/v1/console/overview", headers=headers)
    assert data.status_code == 200
    body = data.json()
    assert body["counts"]["roles"] >= 1
    assert body["counts"]["assets"] >= 1
    assert isinstance(body["notices"], list) and body["notices"]
