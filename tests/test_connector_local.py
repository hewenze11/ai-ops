import json
import os
import sqlite3
import time

import pytest
from ai_ops.app import create_app
from ai_ops import connector_local

from test_control import env, submit, ADMIN


def echo_command(text):
    return "echo " + text


def exit_command(code):
    return "exit %d" % code


def sleep_command(seconds):
    return "sleep %d" % seconds if os.name == "posix" else "ping -n %d 127.0.0.1 >NUL" % (seconds + 1)


def big_output_command():
    if os.name == "posix":
        return "head -c 200000 /dev/zero | tr '\\0' 'a'"
    # Windows: repeatedly echo to exceed the 64 KiB cap without a huge file.
    return "for /L %i in (1,1,40000) do @echo 0123456789012345678901234567890123456789"


def local_asset_id(env):
    c, h, _, _ = env
    listed = c.get("/api/v1/assets", headers=h).json()
    rows = [a for a in listed if a.get("connection_type") == "local"]
    return rows[0]["id"] if rows else None


def test_control_host_registered_by_default(env):
    c, h, _, _ = env
    listed = c.get("/api/v1/assets", headers=h).json()
    locals_ = [a for a in listed if a.get("connection_type") == "local"]
    assert len(locals_) == 1, "the control host must appear in the asset list"
    assert locals_[0]["id"] == connector_local.LOCAL_ASSET_ID
    detail = c.get("/api/v1/local-connector", headers=h).json()
    assert detail["enabled"] is True
    assert detail["asset_id"] == connector_local.LOCAL_ASSET_ID


def test_registration_is_idempotent(env, tmp_path):
    c, h, _, path = env
    # Re-opening the same database must not create a second local asset.
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        connector_local.ensure_local_asset(db)
        connector_local.ensure_local_asset(db)
        count = db.execute("SELECT COUNT(*) FROM local_host").fetchone()[0]
    assert count == 1


def test_local_asset_has_no_usable_agent_token(env):
    c, h, _, _ = env
    aid = local_asset_id(env)
    # Rotating a token on the local asset must be refused (it has no agent).
    resp = c.post("/api/v1/assets/%s/rotate-token" % aid, headers=h, json={"grace_seconds": 0})
    assert resp.status_code == 409
    # And an arbitrary bearer can never authenticate as it.
    claim = c.post("/api/v1/agents/%s/claim" % aid, headers={"Authorization": "Bearer deadbeefdeadbeefdeadbeef"},
                   json={"protocol_version": "1.1"})
    assert claim.status_code in (403, 401)
    # Provisioning must never hand back a token for it.
    assert "agent_token" not in resp.text


def test_local_task_executes_and_is_audited(env):
    c, h, _, path = env
    aid = local_asset_id(env)
    # The default run_as is the current user; its allowed_users holds it.
    detail = c.get("/api/v1/local-connector", headers=h).json()
    run_as = detail["run_as"]
    resp = submit(env, asset_id=aid, execution_users=[run_as], run_as=run_as, command="echo local-ok")
    assert resp.status_code == 201, resp.text
    executor = connector_local.LocalExecutor(_transaction_for(path), _audit_for(path))
    claimed = executor.claim(aid)
    assert claimed is not None and claimed["payload"]["command"] == "echo local-ok"
    result = executor.run(aid, claimed, None)
    assert result["status"] == "succeeded", result
    assert "local-ok" in result["stdout"]
    stored = c.get("/api/v1/tasks/" + claimed["id"], headers=h).json()
    assert stored["state"] == "succeeded"
    assert stored["lease"]["closed"] is True
    events = [e["event"] for e in c.get("/api/v1/audit?limit=200", headers=h).json()]
    assert "task.claimed" in events and "task.result" in events


def test_local_task_rejects_run_as_outside_whitelist(env):
    c, h, _, _ = env
    aid = local_asset_id(env)
    # run_as must be in the asset's allowed_users, exactly like any other asset.
    resp = c.post("/api/v1/tasks", headers=h, json={
        "role_id": "ops", "asset_id": aid, "execution_users": ["nobody-allowed"],
        "run_as": "nobody-allowed", "command": "id", "mode": "direct",
        "idempotency_key": "local-bad-runas-1"})
    assert resp.status_code == 403


def test_local_task_reports_failure_exit_code(env):
    c, h, _, path = env
    aid = local_asset_id(env)
    run_as = c.get("/api/v1/local-connector", headers=h).json()["run_as"]
    submit(env, asset_id=aid, execution_users=[run_as], run_as=run_as, command=exit_command(3), key="local-exit3")
    executor = connector_local.LocalExecutor(_transaction_for(path), _audit_for(path))
    claimed = executor.claim(aid)
    result = executor.run(aid, claimed, None)
    assert result["status"] == "failed" and result["exit_code"] == 3


def test_local_task_cancellation(env):
    c, h, _, path = env
    aid = local_asset_id(env)
    run_as = c.get("/api/v1/local-connector", headers=h).json()["run_as"]
    import threading
    cancel = threading.Event()
    cancel.set()
    submit(env, asset_id=aid, execution_users=[run_as], run_as=run_as, command=sleep_command(30), key="local-cancel")
    executor = connector_local.LocalExecutor(_transaction_for(path), _audit_for(path))
    claimed = executor.claim(aid)
    result = executor.run(aid, claimed, cancel)
    assert result["status"] == "cancelled"
    assert result["error_code"] == "CANCELLED_BY_OPERATOR"


def test_local_task_timeout(env):
    c, h, _, path = env
    aid = local_asset_id(env)
    run_as = c.get("/api/v1/local-connector", headers=h).json()["run_as"]
    c.post("/api/v1/tasks", headers=h, json={
        "role_id": "ops", "asset_id": aid, "execution_users": [run_as], "run_as": run_as,
        "command": sleep_command(30), "timeout_seconds": 1, "mode": "direct",
        "idempotency_key": "local-timeout-1"})
    executor = connector_local.LocalExecutor(_transaction_for(path), _audit_for(path))
    claimed = executor.claim(aid)
    assert claimed is not None
    result = executor.run(aid, claimed, None)
    assert result["status"] == "failed" and result["error_code"] == "EXECUTION_TIMEOUT"


def test_local_output_is_truncated_at_limit(env):
    c, h, _, path = env
    aid = local_asset_id(env)
    run_as = c.get("/api/v1/local-connector", headers=h).json()["run_as"]
    command = big_output_command()
    submit(env, asset_id=aid, execution_users=[run_as], run_as=run_as, command=command, key="local-bigout")
    executor = connector_local.LocalExecutor(_transaction_for(path), _audit_for(path))
    claimed = executor.claim(aid)
    result = executor.run(aid, claimed, None)
    assert len(result["stdout"]) <= connector_local.CHUNK
    assert result["output_truncated"] is True


def test_local_disable_removes_asset_but_keeps_history(env):
    c, h, _, path = env
    aid = local_asset_id(env)
    run_as = c.get("/api/v1/local-connector", headers=h).json()["run_as"]
    task = submit(env, asset_id=aid, execution_users=[run_as], run_as=run_as, command=echo_command("before-disable"), key="local-hist").json()
    executor = connector_local.LocalExecutor(_transaction_for(path), _audit_for(path))
    claimed = executor.claim(aid)
    executor.run(aid, claimed, None)
    assert c.post("/api/v1/local-connector/disable", headers=h).json()["enabled"] is False
    listed = c.get("/api/v1/assets", headers=h).json()
    assert not [a for a in listed if a.get("connection_type") == "local"]
    # The historical task survives: we removed the asset registration, not the facts.
    assert c.get("/api/v1/tasks/" + task["id"], headers=h).json()["state"] == "succeeded"
    # Disabling twice is a conflict, not a crash.
    assert c.post("/api/v1/local-connector/disable", headers=h).status_code == 409


def test_local_can_be_reenabled(env):
    c, h, _, _ = env
    c.post("/api/v1/local-connector/disable", headers=h)
    resp = c.post("/api/v1/local-connector/enable", headers=h)
    assert resp.status_code == 200 and resp.json()["enabled"] is True
    assert local_asset_id(env) == connector_local.LOCAL_ASSET_ID


def test_env_flag_disables_registration(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_OPS_LOCAL_CONNECTOR_ENABLED", "0")
    path = str(tmp_path / "control.db")
    app = create_app(path, ADMIN * 2)
    from fastapi.testclient import TestClient
    c = TestClient(app)
    listed = c.get("/api/v1/assets", headers={"Authorization": "Bearer " + ADMIN * 2}).json()
    assert not [a for a in listed if a.get("connection_type") == "local"]


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
