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
            "ssh_auth_kind": "key", "ssh_secret_ref": secret_ref,
            "ssh_host_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJVEXyRCnfSva5S8OJcOBsqxXneRacM5thAfEH29aexe root@localhost"}
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
    assert "ssh_host_key" not in json.dumps(listed)
    detail = c.get("/api/v1/assets/ssh-host/connection", headers=h).json()
    assert detail["connection_type"] == "ssh" and detail["ssh_host"] == "10.0.0.9"
    assert "ssh_secret_ref" not in detail
    # The pinned key itself is never handed back to the caller either.
    assert "ssh_host_key\"" not in json.dumps(detail)
    assert detail["ssh_host_key_pinned"] is True


def test_ssh_asset_without_pinned_key_is_rejected(env):
    c, h, _, _ = env
    body = {"id": "nopin", "name": "no pin", "allowed_users": ["reader"], "notes": "",
            "connection_type": "ssh", "ssh_host": "10.0.0.9", "ssh_port": 22, "ssh_user": "root",
            "ssh_auth_kind": "key", "ssh_secret_ref": "/tmp/x.secret"}
    assert c.post("/api/v1/assets", headers=h, json=body).status_code == 422


def test_connect_refuses_without_pinned_host_key():
    # Fail closed: no pinned key means we cannot verify the peer, so refuse.
    with pytest.raises(Exception):
        connector_ssh._connect({"ssh_host": "10.0.0.9", "ssh_port": 22, "ssh_user": "root",
                                "ssh_auth_kind": "password", "ssh_secret_ref": "/nope",
                                "ssh_host_key": None})


def test_load_secret_distinguishes_missing_file(tmp_path):
    with pytest.raises(connector_ssh.paramiko.SSHException) as exc:
        connector_ssh._load_secret(str(tmp_path / "does-not-exist"))
    assert "SECRET_NOT_FOUND" in str(exc.value)
    assert connector_ssh._classify_connect_error(exc.value) == "SECRET_NOT_FOUND"


def test_load_secret_distinguishes_unreadable_file(monkeypatch, tmp_path):
    # An unreadable secret (e.g. wrong ownership) must be a distinct, actionable
    # error, not a phantom network failure. Simulate PermissionError directly so
    # the test is meaningful even when running as root/Administrator.
    secret = tmp_path / "locked"
    secret.write_text("x")
    real_open = open

    def fake_open(path, *args, **kwargs):
        if str(path) == str(secret):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    with pytest.raises(connector_ssh.paramiko.SSHException) as exc:
        connector_ssh._load_secret(str(secret))
    assert "SECRET_NOT_READABLE" in str(exc.value)
    assert connector_ssh._classify_connect_error(exc.value) == "SECRET_NOT_READABLE"


def test_classify_connect_error_covers_host_key_cases():
    assert connector_ssh._classify_connect_error(Exception("SSH host key is not pinned for this asset")) == "HOST_KEY_NOT_PINNED"
    assert connector_ssh._classify_connect_error(Exception("pinned SSH host key is not a valid public key")) == "HOST_KEY_INVALID"
    assert connector_ssh._classify_connect_error(Exception("boom")) == "CONNECTION_FAILED"


def test_connect_pins_operator_provided_key(monkeypatch, tmp_path):
    # The exact pinned key must be the only host key the client trusts, and the
    # client must reject anything not pinned in advance.
    secret = tmp_path / "pw.secret"
    secret.write_text("hunter2")
    captured = {}

    class Recorder:
        def __init__(self): self.keys = []
        def get_host_keys(self):
            keys = self.keys
            return type("HK", (), {"add": lambda self, host, kind, key: keys.append((host, kind, str(key)))} )()
        def set_missing_host_key_policy(self, policy): captured["policy"] = policy
        def connect(self, **kwargs): captured["kwargs"] = kwargs
        def close(self): pass

    pinned = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJVEXyRCnfSva5S8OJcOBsqxXneRacM5thAfEH29aexe root@localhost")
    monkeypatch.setattr(connector_ssh.paramiko, "SSHClient", Recorder)
    client = connector_ssh._connect({"ssh_host": "10.0.0.9", "ssh_port": 22, "ssh_user": "root",
                                     "ssh_auth_kind": "password", "ssh_secret_ref": str(secret),
                                     "ssh_host_key": pinned, "ssh_key_type": "ssh-ed25519"})
    assert client.keys and client.keys[0][0] == "10.0.0.9" and client.keys[0][1] == "ssh-ed25519"
    assert isinstance(captured["policy"], connector_ssh.paramiko.RejectPolicy)
    assert captured["kwargs"]["password"] == "hunter2"
    assert captured["kwargs"]["allow_agent"] is False and captured["kwargs"]["look_for_keys"] is False


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
