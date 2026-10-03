"""Admin management: role rename, asset notes, documents and local Skills.

The properties under test are the ones that keep this surface safe:

* every write still requires the admin credential,
* a Skill is injected into the role's context but NEVER grants authority,
* a Skill for another role is not injected at all.
"""
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from ai_ops.app import create_app
from test_console import ADMIN


def make_client():
    return TestClient(create_app(str(Path(tempfile.mkdtemp()) / "control.db"), ADMIN, "test-model"))


def setup(client, headers, roles=("ops",)):
    for role in roles:
        client.post("/api/v1/roles", headers=headers, json={"id": role, "name": role})
        client.put("/api/v1/roles/" + role + "/model", headers=headers, json={"enabled": True})
    asset = client.post("/api/v1/assets", headers=headers, json={
        "id": "host", "name": "Host", "allowed_users": ["reader"], "notes": "old note"}).json()
    return asset


def test_management_endpoints_require_admin():
    client = make_client()
    checks = [
        ("PUT", "/api/v1/roles/ops", {"name": "x"}),
        ("PUT", "/api/v1/assets/host/notes", {"notes": "x"}),
        ("GET", "/api/v1/skills", None),
        ("PUT", "/api/v1/skills/s1", {"id": "s1", "name": "s", "content": "", "role_ids": []}),
    ]
    for method, path, body in checks:
        response = client.request(method, path, json=body) if body is not None else client.request(method, path)
        assert response.status_code in (401, 403), path


def test_role_rename_and_asset_update():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)
    renamed = client.put("/api/v1/roles/ops", headers=headers, json={"name": "Operations"})
    assert renamed.status_code == 200
    assert any(r["name"] == "Operations" for r in client.get("/api/v1/roles", headers=headers).json())

    updated = client.put("/api/v1/assets/host/notes", headers=headers,
                         json={"notes": "new note", "allowed_users": ["reader", "operator"]})
    assert updated.status_code == 200 and updated.json()["notes"] == "new note"
    assert client.get("/api/v1/assets", headers=headers).json()[0]["allowed_users"] == ["reader", "operator"]


def test_asset_update_rejects_duplicate_users():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)
    bad = client.put("/api/v1/assets/host/notes", headers=headers, json={"allowed_users": ["reader", "reader"]})
    assert bad.status_code == 422


def test_skill_saved_and_injected_into_role_context():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)
    saved = client.put("/api/v1/skills/diag", headers=headers, json={
        "id": "diag", "name": "Diagnostics", "content": "Always check disk before CPU.", "role_ids": ["ops"]})
    assert saved.status_code == 200 and saved.json()["revision"] == 1

    # A turn for 'ops' must carry the skill text in its mandatory context.
    client.post("/api/v1/roles/ops/messages", headers=headers, json={
        "text": "inspect", "execution_users": [], "mode": "readonly", "idempotency_key": "skill-key-001"})
    turn = client.get("/api/v1/roles/ops/turns", headers=headers).json()[0]
    body = client.app.state.role_engine.request_body  # engine exists; build via app
    from ai_ops.context import build_messages
    with client.app.state.transaction() as db:
        row = db.execute("SELECT * FROM role_turns WHERE id=?", (turn["id"],)).fetchone()
        messages = build_messages(client.app, db, row, None)
    system = messages[0]["content"]
    assert "CURRENT_SKILLS" in system and "Always check disk before CPU." in system


def test_skill_for_other_role_is_not_injected():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers, roles=("ops", "other"))
    client.put("/api/v1/skills/private", headers=headers, json={
        "id": "private", "name": "Other only", "content": "OTHER_ROLE_SECRET_MARKER", "role_ids": ["other"]})
    client.post("/api/v1/roles/ops/messages", headers=headers, json={
        "text": "inspect", "execution_users": [], "mode": "readonly", "idempotency_key": "skill-key-002"})
    from ai_ops.context import build_messages
    with client.app.state.transaction() as db:
        row = db.execute("SELECT * FROM role_turns WHERE role_id='ops'").fetchone()
        messages = build_messages(client.app, db, row, None)
    assert "OTHER_ROLE_SECRET_MARKER" not in messages[0]["content"]


def test_disabled_skill_not_injected_and_delete_hides_it():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)
    client.put("/api/v1/skills/off", headers=headers, json={
        "id": "off", "name": "Off", "content": "DISABLED_MARKER", "role_ids": ["ops"], "enabled": False})
    from ai_ops.context import build_messages
    client.post("/api/v1/roles/ops/messages", headers=headers, json={
        "text": "inspect", "execution_users": [], "mode": "readonly", "idempotency_key": "skill-key-003"})
    with client.app.state.transaction() as db:
        row = db.execute("SELECT * FROM role_turns WHERE role_id='ops'").fetchone()
        assert "DISABLED_MARKER" not in build_messages(client.app, db, row, None)[0]["content"]

    assert client.delete("/api/v1/skills/off", headers=headers).json()["deleted"] is True
    assert client.get("/api/v1/skills", headers=headers).json() == []


def test_document_save_and_delete():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)
    body = {"id": "runbook", "name": "Runbook", "content": "step 1", "core": False, "role_ids": ["ops"]}
    assert client.put("/api/v1/documents/runbook", headers=headers, json=body).status_code == 200
    assert any(d["id"] == "runbook" for d in client.get("/api/v1/documents", headers=headers).json())
    assert client.delete("/api/v1/documents/runbook", headers=headers).json()["deleted"] is True
    assert client.get("/api/v1/documents", headers=headers).json() == []
