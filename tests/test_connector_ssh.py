import json
import os
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from ai_ops.app import create_app
from ai_ops import connector_ssh
from test_control import env, submit, claim, finish, ADMIN


def ssh_asset(env, asset_id="ssh-host", secret_ref=None):
    c, h, _, _ = env
    secret_ref = secret_ref or (str(os.path.abspath(__file__)) + ".secret")
    with open(secret_ref, "w") as handle:
        handle.write("not-a-real-key")
    body = {"id": asset_id, "name": "SSH host", "allowed_users": ["reader", "operator"], "notes": "ssh",
            "connection_type": "ssh", "ssh_host": "10.0.0.9", "ssh_port": 22, "ssh_user": "root",
            "ssh_auth_kind": "key", "ssh_secret_ref": secret_ref}
    response = c.post("/api/v1/assets", headers=h, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_ssh_asset_requires_endpoint_fields(env):
    c, h, _, _ = env
    body = {"id": "bad", "name": "bad", "allowed_users": ["reader"], "notes": "",
            "connection_type": "ssh"}
    assert c.post("/api/v1/assets", headers=h, json=body).status_code == 422


def test_ssh_asset_metadata_never_exposes_secret_ref(env):
    c, h, _, _ = env
    provisioned = ssh_asset(env)
    assert provisioned["connection_type"] == "ssh" and "agent_token" not in provisioned
    listed = c.get("/api/v1/assets", headers=h).json()
    row = [a for a in listed if a["id"] == "ssh-host"][0]
    assert row["connection_type"] == "ssh"
    assert "ssh_secret_ref" not in json.dumps(listed)
    detail = c.get("/api/v1/assets/ssh-host/connection", headers=h).json()
    assert detail["connection_type"] == "ssh" and detail["ssh_host"] == "10.0.0.9"
    assert "ssh_secret_ref" not in detail


class FakeChannel:
    def __init__(self, stdout=b"", stderr=b"", exit_status=0):
        self._out, self._err, self._exit = bytearray(stdout), bytearray(stderr), exit_status
        self.closed = False

    def settimeout(self, _): pass
    def exec_command(self, _): pass
    def recv_ready(self): return bool(self._out)
    def recv(self, n):
        data = bytes(self._out[:n]); del self._out[:n]; return data
    def recv_stderr_ready(self): return bool(self._err)
    def recv_stderr(self, n):
        data = bytes(self._err[:n]); del self._err[:n]; return data
    def exit_status_ready(self): return not self._out and not self._err
    def recv_exit_status(self): return self._exit
    def close(self): self.closed = True


class FakeClient:
    def __init__(self, channel): self._channel = channel
    def get_transport(self):
        channel = self._channel
        return type("T", (), {"open_session": lambda self, timeout=None: channel})()
    def close(self): pass


def test_executor_records_success_and_closes_lease(env, monkeypatch):
    c, h, tokens, path = env
    ssh_asset(env)
    channel = FakeChannel(stdout=b"hello\n", exit_status=0)
    monkeypatch.setattr(connector_ssh, "_connect", lambda info: FakeClient(channel))
    task = submit(env, asset_id="ssh-host")
    executor = connector_ssh.ConnectorExecutor(_transaction_for(path), _audit_for(path))
    claimed = executor.claim("ssh-host")
    assert claimed is not None
    result = executor.run("ssh-host", claimed, None)
    assert result["status"] == "succeeded" and result["stdout"].strip() == "hello"
    stored = c.get("/api/v1/tasks/" + claimed["id"], headers=h).json()
    assert stored["state"] == "succeeded"
    assert stored["lease"]["closed"] is True


def test_executor_reports_unknown_when_connection_drops(env, monkeypatch):
    c, h, _, path = env
    ssh_asset(env)

    class Dying(FakeChannel):
        def exit_status_ready(self):
            self.closed = True
            return False

    monkeypatch.setattr(connector_ssh, "_connect", lambda info: FakeClient(Dying(stdout=b"partial")))
    task = submit(env, asset_id="ssh-host")
    executor = connector_ssh.ConnectorExecutor(_transaction_for(path), _audit_for(path))
    claimed = executor.claim("ssh-host")
    result = executor.run("ssh-host", claimed, None)
    assert result["status"] == "unknown" and result["error_code"] == "CONNECTION_LOST_DURING_EXECUTION"
    assert c.get("/api/v1/tasks/" + claimed["id"], headers=h).json()["state"] == "unknown"


def _transaction_for(path):
    import contextlib

    @contextlib.contextmanager
    def transaction():
        db = sqlite3.connect(path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
    return transaction


def _audit_for(path):
    def audit(db, event, entity_id, actor, details):
        db.execute("INSERT INTO audit(event,entity_id,actor,details,created_at) VALUES(?,?,?,?,?)",
                   (event, entity_id, actor, json.dumps(details, ensure_ascii=False), time.time()))
    return audit
