import base64
import hashlib
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from ai_ops.app import create_app
from ai_ops import retention

from test_control import ADMIN


def make_env(tmp_path, monkeypatch, **policy):
    for name, value in policy.items():
        monkeypatch.setenv(name, str(value))
    path = tmp_path / "retention.db"
    client = TestClient(create_app(str(path), ADMIN))
    headers = {"Authorization": "Bearer " + ADMIN}
    assert client.post("/api/v1/roles", headers=headers, json={"id": "ops", "name": "Ops"}).status_code == 201
    token = client.post("/api/v1/assets", headers=headers, json={
        "id": "a", "name": "a", "allowed_users": ["reader"], "notes": ""}).json()["agent_token"]
    return client, headers, {"Authorization": "Bearer " + token}, path


def _submit(client, headers, key, command="id", role_id="ops"):
    return client.post("/api/v1/tasks", headers=headers, json={
        "role_id": role_id, "asset_id": "a", "execution_users": ["reader"], "run_as": "reader",
        "command": command, "idempotency_key": key}).json()


def _claim(client, agent_headers):
    return client.post("/api/v1/agents/a/claim", headers=agent_headers,
                       json={"protocol_version": "1.1"}).json()["task"]


def _archive(client, agent_headers, task, payload, stream="stdout"):
    client.post("/api/v1/agents/a/tasks/" + task["id"] + "/output/" + stream + "/chunks",
                headers=agent_headers, json={"claim_id": task["claim_id"], "offset": 0,
                                             "data": base64.b64encode(payload).decode()})
    client.post("/api/v1/agents/a/tasks/" + task["id"] + "/output/" + stream + "/finalize",
                headers=agent_headers, json={"claim_id": task["claim_id"], "size": len(payload),
                                             "sha256": hashlib.sha256(payload).hexdigest(),
                                             "complete": True})


def _settle(client, agent_headers, task, status="succeeded"):
    return client.post("/api/v1/agents/a/tasks/" + task["id"] + "/result", headers=agent_headers,
                       json={"claim_id": task["claim_id"], "status": status,
                             "exit_code": 0 if status == "succeeded" else None, "stdout": "ok"})


def build_settled_task(client, headers, agent_headers, key, payload=b"x" * 100, status="succeeded"):
    _submit(client, headers, key)
    task = _claim(client, agent_headers)
    _archive(client, agent_headers, task, payload)
    _settle(client, agent_headers, task, status)
    return task


def test_policy_refuses_invalid_values():
    with pytest.raises(ValueError):
        retention.policy_from_env({"AI_OPS_OUTPUT_MAX_BYTES": "-1"})
    with pytest.raises(ValueError):
        retention.policy_from_env({"AI_OPS_OUTPUT_RETENTION_DAYS": "abc"})
    policy = retention.policy_from_env({})
    assert policy == {"max_bytes": retention.DEFAULT_MAX_BYTES,
                      "retention_days": retention.DEFAULT_RETENTION_DAYS,
                      "keep_tasks": retention.DEFAULT_KEEP_TASKS}


def test_usage_reports_totals(tmp_path, monkeypatch):
    client, headers, agent_headers, _ = make_env(tmp_path, monkeypatch)
    build_settled_task(client, headers, agent_headers, "key-0001", payload=b"a" * 40)
    body = client.get("/api/v1/output/usage", headers=headers).json()
    assert body["total_bytes"] == 40 and body["archives"] == 1
    assert body["policy"]["max_bytes"] == retention.DEFAULT_MAX_BYTES


def test_prune_deletes_archives_past_the_age_window(tmp_path, monkeypatch):
    client, headers, agent_headers, path = make_env(tmp_path, monkeypatch,
                                                    AI_OPS_OUTPUT_RETENTION_DAYS=1,
                                                    AI_OPS_OUTPUT_TASK_KEEP=0)
    task = build_settled_task(client, headers, agent_headers, "key-old1", payload=b"y" * 30)
    # Backdate the task so it is well outside the one-day window.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE tasks SET created_at=?, updated_at=? WHERE id=?",
                   (time.time() - 5 * 86400, time.time() - 5 * 86400, task["id"]))
    result = client.post("/api/v1/output/prune", headers=headers).json()
    assert result["deleted_tasks"] == 1 and result["deleted_bytes"] == 30
    assert client.get("/api/v1/output/usage", headers=headers).json()["total_bytes"] == 0


def test_prune_keeps_recent_tasks_inside_window(tmp_path, monkeypatch):
    client, headers, agent_headers, path = make_env(tmp_path, monkeypatch,
                                                    AI_OPS_OUTPUT_RETENTION_DAYS=14,
                                                    AI_OPS_OUTPUT_TASK_KEEP=0)
    build_settled_task(client, headers, agent_headers, "key-recent", payload=b"z" * 25)
    result = client.post("/api/v1/output/prune", headers=headers).json()
    assert result["deleted_tasks"] == 0
    assert client.get("/api/v1/output/usage", headers=headers).json()["total_bytes"] == 25


def test_keep_floor_protects_newest_tasks_even_when_old(tmp_path, monkeypatch):
    client, headers, agent_headers, path = make_env(tmp_path, monkeypatch,
                                                    AI_OPS_OUTPUT_RETENTION_DAYS=1,
                                                    AI_OPS_OUTPUT_TASK_KEEP=2)
    tasks = [build_settled_task(client, headers, agent_headers, "key-k%03d" % i, payload=b"q" * 10)
             for i in range(3)]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE tasks SET created_at=?, updated_at=?",
                   (time.time() - 9 * 86400, time.time() - 9 * 86400))
    result = client.post("/api/v1/output/prune", headers=headers).json()
    # Three tasks, floor of two keeps the newest two, only the oldest is pruned.
    assert result["deleted_tasks"] == 1
    remaining = client.get("/api/v1/output/usage", headers=headers).json()
    assert remaining["total_bytes"] == 20 and remaining["archives"] == 2


def test_byte_budget_evicts_oldest_even_inside_window(tmp_path, monkeypatch):
    # Build five archives under a generous cap, then apply a tighter cap so the
    # pruner has to evict the oldest even though they are inside the age window.
    client, headers, agent_headers, path = make_env(tmp_path, monkeypatch,
                                                    AI_OPS_OUTPUT_MAX_BYTES=10_000,
                                                    AI_OPS_OUTPUT_RETENTION_DAYS=3650,
                                                    AI_OPS_OUTPUT_TASK_KEEP=0)
    for i in range(5):  # 5 * 100 = 500 bytes stored
        build_settled_task(client, headers, agent_headers, "key-b%03d" % i, payload=b"m" * 100)
        time.sleep(0.01)  # distinct created_at ordering
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        result = retention.prune(db, lambda *a, **k: None,
                                 {"max_bytes": 250, "retention_days": 3650, "keep_tasks": 0})
    assert result["deleted_tasks"] == 3 and result["deleted_bytes"] == 300
    assert client.get("/api/v1/output/usage", headers=headers).json()["total_bytes"] == 200


def test_in_flight_and_unknown_output_are_never_pruned(tmp_path, monkeypatch):
    client, headers, agent_headers, path = make_env(tmp_path, monkeypatch,
                                                    AI_OPS_OUTPUT_MAX_BYTES=10_000,
                                                    AI_OPS_OUTPUT_RETENTION_DAYS=0,
                                                    AI_OPS_OUTPUT_TASK_KEEP=0)
    # A claimed (in-flight) task: archive finalized but task not settled yet.
    _submit(client, headers, "key-live")
    live = _claim(client, agent_headers)
    _archive(client, agent_headers, live, b"live-output")
    # An unknown task on a second role so it does not block the first role's
    # serial queue; its settled raw output is still needed for resolution.
    client.post("/api/v1/roles", headers=headers, json={"id": "ops2", "name": "Ops two"})
    _submit(client, headers, "key-unkn", role_id="ops2")
    unknown = _claim(client, agent_headers)
    _archive(client, agent_headers, unknown, b"unknown-output")
    _settle(client, agent_headers, unknown, status="unknown")

    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        result = retention.prune(db, lambda *a, **k: None,
                                 {"max_bytes": 0, "retention_days": 0, "keep_tasks": 0})
    assert result["deleted_tasks"] == 0
    usage = client.get("/api/v1/output/usage", headers=headers).json()
    assert usage["total_bytes"] == len(b"live-output") + len(b"unknown-output")


def test_budget_guard_blocks_new_chunks_when_over_capacity(tmp_path, monkeypatch):
    client, headers, agent_headers, _ = make_env(tmp_path, monkeypatch,
                                                 AI_OPS_OUTPUT_MAX_BYTES=50)
    build_settled_task(client, headers, agent_headers, "key-seed", payload=b"s" * 50)
    _submit(client, headers, "key-more")
    task = _claim(client, agent_headers)
    response = client.post("/api/v1/agents/a/tasks/" + task["id"] + "/output/stdout/chunks",
                           headers=agent_headers,
                           json={"claim_id": task["claim_id"], "offset": 0,
                                 "data": base64.b64encode(b"n" * 10).decode()})
    assert response.status_code == 413


def test_budget_guard_allows_a_task_to_use_its_own_quota(tmp_path, monkeypatch):
    # A single task may fill up to the budget; its own earlier chunks do not
    # count against it.
    client, headers, agent_headers, _ = make_env(tmp_path, monkeypatch,
                                                 AI_OPS_OUTPUT_MAX_BYTES=100)
    _submit(client, headers, "key-big1")
    task = _claim(client, agent_headers)
    for offset in (0, 50):
        response = client.post("/api/v1/agents/a/tasks/" + task["id"] + "/output/stdout/chunks",
                               headers=agent_headers,
                               json={"claim_id": task["claim_id"], "offset": offset,
                                     "data": base64.b64encode(b"p" * 50).decode()})
        assert response.status_code == 200, response.text
    # Settle it so the next task can be claimed (one outstanding task per role),
    # then confirm the store is at capacity and refuses further uploads.
    _settle(client, agent_headers, task)
    _submit(client, headers, "key-secnd")
    second = _claim(client, agent_headers)
    blocked = client.post("/api/v1/agents/a/tasks/" + second["id"] + "/output/stdout/chunks",
                          headers=agent_headers,
                          json={"claim_id": second["claim_id"], "offset": 0,
                                "data": base64.b64encode(b"n").decode()})
    assert blocked.status_code == 413


def test_prune_is_audited(tmp_path, monkeypatch):
    client, headers, agent_headers, path = make_env(tmp_path, monkeypatch,
                                                    AI_OPS_OUTPUT_RETENTION_DAYS=0,
                                                    AI_OPS_OUTPUT_TASK_KEEP=0)
    build_settled_task(client, headers, agent_headers, "key-aud1", payload=b"a" * 10)
    client.post("/api/v1/output/prune", headers=headers)
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT details FROM audit WHERE event='output.pruned'").fetchone()
    assert row is not None and json.loads(row[0])["bytes"] == 10


def test_prune_endpoint_requires_admin(tmp_path, monkeypatch):
    client, headers, agent_headers, _ = make_env(tmp_path, monkeypatch)
    assert client.post("/api/v1/output/prune").status_code == 401
    assert client.post("/api/v1/output/prune", headers=agent_headers).status_code == 403
