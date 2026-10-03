import os
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from ai_ops import admin_cli
from ai_ops.app import create_app
from test_control import env, submit, claim, ADMIN


def _auth(token):
    return {"Authorization": "Bearer " + token}


def _claim(client, token, asset="a"):
    return client.post("/api/v1/agents/" + asset + "/claim", headers=_auth(token),
                       json={"protocol_version": "1.0"})


def test_rotation_invalidates_the_old_token_immediately(env):
    c, h, tokens, path = env
    old = tokens["a"]["Authorization"][7:]
    rotate = c.post("/api/v1/assets/a/rotate-token", headers=h, json={"grace_seconds": 0})
    assert rotate.status_code == 200, rotate.text
    new = rotate.json()["agent_token"]
    assert new != old
    # The old token can no longer claim; the new one can.
    assert _claim(c, old).status_code == 403
    assert _claim(c, new).status_code == 200


def test_rotation_grace_window_keeps_old_token_usable(env):
    c, h, tokens, _ = env
    old = tokens["a"]["Authorization"][7:]
    new = c.post("/api/v1/assets/a/rotate-token", headers=h, json={"grace_seconds": 3600}).json()["agent_token"]
    # Both tokens work during the overlap so a rollout can switch safely.
    assert _claim(c, old).status_code == 200
    assert _claim(c, new).status_code == 200


def test_expired_grace_window_rejects_the_old_token(env):
    c, h, tokens, path = env
    old = tokens["a"]["Authorization"][7:]
    new = c.post("/api/v1/assets/a/rotate-token", headers=h, json={"grace_seconds": 60}).json()["agent_token"]
    # Simulate the grace window elapsing.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE assets SET previous_token_expires=? WHERE id='a'", (time.time() - 1,))
    assert _claim(c, old).status_code == 403
    assert _claim(c, new).status_code == 200


def test_rotation_rejects_ssh_assets(env):
    c, h, _, _ = env
    body = {"id": "ssh1", "name": "ssh", "allowed_users": ["reader"], "notes": "",
            "connection_type": "ssh", "ssh_host": "10.0.0.9", "ssh_port": 22, "ssh_user": "root",
            "ssh_auth_kind": "key", "ssh_secret_ref": "/tmp/k",
            "ssh_host_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJVEXyRCnfSva5S8OJcOBsqxXneRacM5thAfEH29aexe root@localhost"}
    assert c.post("/api/v1/assets", headers=h, json=body).status_code == 201
    assert c.post("/api/v1/assets/ssh1/rotate-token", headers=h, json={"grace_seconds": 0}).status_code == 409


def test_rotation_requires_admin(env):
    c, _, tokens, _ = env
    assert c.post("/api/v1/assets/a/rotate-token", json={"grace_seconds": 0}).status_code == 401
    assert c.post("/api/v1/assets/a/rotate-token", headers=tokens["a"], json={"grace_seconds": 0}).status_code == 403


def test_rotation_audit_never_contains_the_token(env):
    c, h, _, path = env
    new = c.post("/api/v1/assets/a/rotate-token", headers=h, json={"grace_seconds": 0}).json()["agent_token"]
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT details FROM audit WHERE event='asset.token_rotated'").fetchall()
    assert rows and new not in rows[0][0]


def test_asset_detail_reports_grace_state(env):
    c, h, _, _ = env
    detail = c.get("/api/v1/assets/a", headers=h).json()
    assert detail["previous_token_active"] is False
    c.post("/api/v1/assets/a/rotate-token", headers=h, json={"grace_seconds": 600})
    detail = c.get("/api/v1/assets/a", headers=h).json()
    assert detail["previous_token_active"] is True
    assert detail["previous_token_expires"] > time.time()


def test_rotation_rejects_unknown_asset(env):
    c, h, _, _ = env
    assert c.post("/api/v1/assets/missing/rotate-token", headers=h, json={"grace_seconds": 0}).status_code == 404


def test_admin_token_is_loaded_fresh_after_rotation(tmp_path):
    path = tmp_path / "control.db"
    token_file = tmp_path / "admin_token"
    token_file.write_text("A" * 40)
    client = TestClient(create_app(str(path), "A" * 40, admin_token_file=str(token_file)))
    # Rotate by replacing the file: the running app must honour the new token
    # without a restart, and reject the old one.
    token_file.write_text("B" * 40)
    assert client.get("/api/v1/assets", headers=_auth("B" * 40)).status_code == 200
    assert client.get("/api/v1/assets", headers=_auth("A" * 40)).status_code == 403


def test_admin_token_provider_falls_back_when_file_disappears(tmp_path):
    path = tmp_path / "control.db"
    token_file = tmp_path / "admin_token"
    token_file.write_text("C" * 40)
    client = TestClient(create_app(str(path), "C" * 40, admin_token_file=str(token_file)))
    os.unlink(token_file)
    # Losing the file must not lock the operator out; the startup token still works.
    assert client.get("/api/v1/assets", headers=_auth("C" * 40)).status_code == 200


def test_admin_token_cli_rotates_and_keeps_backup(tmp_path):
    target = tmp_path / "admin_token"
    target.write_text("old-token-value-000000000000000000")
    result = admin_cli.rotate(str(target))
    new_value = target.read_text()
    assert len(new_value) >= 32 and new_value != "old-token-value-000000000000000000"
    assert result["backup"] and os.path.exists(result["backup"])
    assert open(result["backup"]).read() == "old-token-value-000000000000000000"
    # POSIX reports the mode directly; Windows cannot express 0600 via chmod
    # (os.chmod only toggles the read-only bit), so don't assert it there.
    if os.name == "posix":
        assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_admin_token_cli_refuses_without_path():
    assert admin_cli.main(["--path", ""]) == 2
