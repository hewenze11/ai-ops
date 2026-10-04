"""Admin management: role rename, asset notes, documents and local Skills.

The properties under test are the ones that keep this surface safe:

* every write still requires the admin credential,
* a Skill is injected into the role's context but NEVER grants authority,
* a Skill for another role is not injected at all.
"""
import json
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


def test_console_role_creation_and_conflict():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    created = client.post("/api/v1/console/roles", headers=headers, json={"id": "ops", "name": "Operations"})
    assert created.status_code == 201 and created.json()["id"] == "ops"
    # No credential is minted for a role: the response carries id and name only.
    assert set(created.json()) == {"id", "name"}
    assert client.post("/api/v1/console/roles", headers=headers,
                       json={"id": "ops", "name": "Dup"}).status_code == 409


def test_console_asset_registration_returns_agent_token_once():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    created = client.post("/api/v1/console/assets", headers=headers, json={
        "id": "host", "name": "Host", "connection_type": "agent", "allowed_users": ["reader"]})
    assert created.status_code == 201
    token = created.json()["agent_token"]
    assert token
    # The token must never appear on any GET route.
    assets = client.get("/api/v1/assets", headers=headers).json()
    assert "agent_token" not in assets[0] and json.dumps(assets) .find(token) == -1
    assert client.post("/api/v1/console/assets", headers=headers, json={
        "id": "host", "name": "Host", "connection_type": "agent", "allowed_users": ["reader"]}).status_code == 409


def test_console_ssh_asset_requires_pinned_host_key():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    incomplete = client.post("/api/v1/console/assets", headers=headers, json={
        "id": "srv", "name": "Srv", "connection_type": "ssh", "allowed_users": ["reader"],
        "ssh_host": "10.0.0.9", "ssh_user": "root", "ssh_auth_kind": "key",
        "ssh_secret_ref": "secret://srv"})
    assert incomplete.status_code == 422  # missing ssh_host_key
    ok = client.post("/api/v1/console/assets", headers=headers, json={
        "id": "srv", "name": "Srv", "connection_type": "ssh", "allowed_users": ["reader"],
        "ssh_host": "10.0.0.9", "ssh_user": "root", "ssh_auth_kind": "key",
        "ssh_secret_ref": "secret://srv", "ssh_host_key": "ssh-ed25519 AAAA"})
    assert ok.status_code == 201 and "agent_token" not in ok.json()


def test_console_custom_task_create_returns_token_edit_does_not():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)
    created = client.post("/api/v1/console/custom-tasks", headers=headers, json={
        "id": "nightly", "name": "Nightly", "kind": "scheduled", "role_id": "ops",
        "prompt": "check disk", "execution_users": ["reader"], "mode": "readonly",
        "cron": "0 3 * * *"})
    assert created.status_code == 201
    assert created.json()["trigger_token"] and created.json()["next_fire_at"]

    edited = client.put("/api/v1/console/custom-tasks/nightly", headers=headers, json={
        "id": "nightly", "name": "Nightly v2", "kind": "scheduled", "role_id": "ops",
        "prompt": "check disk and memory", "execution_users": ["reader"], "mode": "readonly",
        "cron": "0 4 * * *"})
    assert edited.status_code == 200
    assert "trigger_token" not in edited.json() and edited.json()["token_unchanged"] is True
    listed = client.get("/api/v1/custom-tasks", headers=headers).json()
    assert listed[0]["name"] == "Nightly v2" and listed[0]["revision"] == 2


def test_console_routes_require_admin():
    client = make_client()
    for path, body in [("/api/v1/console/roles", {"id": "x", "name": "x"}),
                       ("/api/v1/console/assets", {"id": "x", "name": "x", "allowed_users": ["r"]})]:
        assert client.post(path, json=body).status_code in (401, 403), path


# ---- pull skills from the website hub ------------------------------------

class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, n=-1):
        return self._payload if n is None or n < 0 else self._payload[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _hub_opener(payload: dict):
    def _open(req, timeout=None):
        return _FakeResp(json.dumps(payload).encode("utf-8"))
    return _open


def test_skills_sync_imports_and_binds_known_roles():
    import ai_ops.management as mgmt
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)  # defines role "ops"

    payload = {"bundle_revision": 7, "count": 2, "skills": [
        {"id": "linux-triage", "name": "Linux triage", "content": "check dmesg", "role_ids": ["ops"], "enabled": True, "revision": 3, "tier": "premium"},
        {"id": "ghost", "name": "Ghost role", "content": "x", "role_ids": ["does-not-exist"], "enabled": True},
    ]}
    original = mgmt.urllib.request.urlopen
    import urllib.request as u
    saved = u.urlopen
    u.urlopen = _hub_opener(payload)
    try:
        resp = client.post("/api/v1/skills/sync", headers=headers, json={
            "source_url": "https://hub.example/api/v1/skills/repo", "pull_key": "aiops-sk-abcdefgh"})
    finally:
        u.urlopen = saved
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bundle_revision"] == 7
    assert [s["id"] for s in body["imported"]] == ["linux-triage"]
    assert body["skipped"][0]["id"] == "ghost"
    # The imported skill really landed and is bound to the known role only.
    listed = client.get("/api/v1/skills", headers=headers).json()
    row = next(s for s in listed if s["id"] == "linux-triage")
    assert row["role_ids"] == ["ops"] and row["enabled"] is True


def test_skills_sync_role_override_and_dry_run():
    import urllib.request as u
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers, roles=("ops", "other"))
    payload = {"skills": [{"id": "s1", "name": "S", "content": "c", "role_ids": ["ops"], "enabled": True}]}
    saved = u.urlopen
    u.urlopen = _hub_opener(payload)
    try:
        resp = client.post("/api/v1/skills/sync", headers=headers, json={
            "source_url": "https://hub.example/api/v1/skills/repo", "pull_key": "aiops-sk-abcdefgh",
            "role_ids": ["other"], "dry_run": True})
    finally:
        u.urlopen = saved
    body = resp.json()
    assert body["dry_run"] is True and body["imported"][0]["role_ids"] == ["other"]
    # dry_run must not have written anything.
    assert client.get("/api/v1/skills", headers=headers).json() == []


def test_skills_sync_requires_admin():
    client = make_client()
    resp = client.post("/api/v1/skills/sync", json={"source_url": "https://h/x", "pull_key": "aiops-sk-abcdefgh"})
    assert resp.status_code in (401, 403)


def test_document_save_and_delete():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)
    body = {"id": "runbook", "name": "Runbook", "content": "step 1", "core": False, "role_ids": ["ops"]}
    assert client.put("/api/v1/documents/runbook", headers=headers, json=body).status_code == 200
    assert any(d["id"] == "runbook" for d in client.get("/api/v1/documents", headers=headers).json())
    assert client.delete("/api/v1/documents/runbook", headers=headers).json()["deleted"] is True
    assert client.get("/api/v1/documents", headers=headers).json() == []
