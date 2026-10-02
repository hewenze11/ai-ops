import sqlite3
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from ai_ops.app import create_app

ADMIN = "test-admin-credential-not-real-1234567890"


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "test.db"
    client = TestClient(create_app(str(path), ADMIN))
    headers = {"Authorization": "Bearer " + ADMIN}
    assert client.post("/api/v1/roles", headers=headers, json={"id": "ops", "name": "Ops"}).status_code == 201
    tokens = {}
    for asset in ("a", "b"):
        r = client.post("/api/v1/assets", headers=headers, json={"id": asset, "name": asset, "allowed_users": ["reader", "operator"], "notes": "test"})
        assert r.status_code == 201
        tokens[asset] = {"Authorization": "Bearer " + r.json()["agent_token"]}
    return client, headers, tokens, path


def submit(env, key="request-001", **kwargs):
    c, h, _, _ = env
    data = {"role_id": "ops", "asset_id": "a", "execution_users": ["reader"], "run_as": "reader", "command": "id", "idempotency_key": key}
    data.update(kwargs)
    return c.post("/api/v1/tasks", headers=h, json=data)


def claim(env, asset="a"):
    c, _, tokens, _ = env
    return c.post(f"/api/v1/agents/{asset}/claim", headers=tokens[asset], json={"protocol_version": "1.0"})


def finish(env, task, status="succeeded"):
    c, _, tokens, _ = env
    return c.post(f"/api/v1/agents/{task['asset_id']}/tasks/{task['id']}/result", headers=tokens[task["asset_id"]], json={"claim_id": task["claim_id"], "status": status, "exit_code": 0 if status == "succeeded" else None, "stdout": "ok"})


def test_admin_required(env):
    c, _, tokens, _ = env
    assert c.get("/api/v1/assets").status_code == 401
    assert c.get("/api/v1/assets", headers=tokens["a"]).status_code == 403


def test_missing_secret_rejected(tmp_path):
    with pytest.raises(ValueError):
        create_app(str(tmp_path / "x.db"), "")


def test_account_not_in_round_rejected(env):
    assert submit(env, run_as="operator").status_code == 422


def test_account_not_on_asset_rejected(env):
    assert submit(env, run_as="root", execution_users=["root"]).status_code == 403


def test_selection_snapshot_and_success(env):
    c, h, _, _ = env
    r = submit(env, execution_users=["reader", "operator"])
    assert r.status_code == 201
    task = claim(env).json()["task"]
    assert task["execution_users"] == ["reader", "operator"]
    assert task["run_as"] == "reader"
    assert finish(env, task).status_code == 200
    assert c.get("/api/v1/tasks/" + task["id"], headers=h).json()["state"] == "succeeded"


def test_fifo_and_cross_asset_head_block(env):
    submit(env, asset_id="b")
    submit(env, key="request-002", asset_id="a")
    assert claim(env, "a").json()["task"] is None
    first = claim(env, "b").json()["task"]
    assert claim(env, "a").json()["task"] is None
    finish(env, first)
    assert claim(env, "a").json()["task"] is not None


def test_roles_can_progress_independently(env):
    c, h, _, _ = env
    c.post("/api/v1/roles", headers=h, json={"id": "db", "name": "DB"})
    submit(env, asset_id="b")
    submit(env, key="request-002", role_id="db")
    assert claim(env).json()["task"] is not None


def test_approval_not_ordinary_queue_task(env):
    c, h, _, _ = env
    task_id = submit(env, mode="confirm").json()["id"]
    assert claim(env).json()["task"] is None
    assert c.post(f"/api/v1/tasks/{task_id}/approve", headers=h).status_code == 200
    assert claim(env).json()["task"]["id"] == task_id


def test_cancel_only_unclaimed(env):
    c, h, _, _ = env
    task_id = submit(env).json()["id"]
    assert c.post(f"/api/v1/tasks/{task_id}/cancel", headers=h).status_code == 200
    assert claim(env).json()["task"] is None
    other = submit(env, key="request-002").json()["id"]
    claim(env)
    assert c.post(f"/api/v1/tasks/{other}/cancel", headers=h).status_code == 409


def test_idempotent_submission_and_result(env):
    first = submit(env).json()
    assert submit(env).json()["id"] == first["id"]
    assert submit(env, command="whoami").status_code == 409
    task = claim(env).json()["task"]
    assert finish(env, task).json()["duplicate"] is False
    assert finish(env, task).json()["duplicate"] is True
    assert finish(env, task, "failed").status_code == 409


def test_wrong_asset_and_protocol(env):
    c, _, tokens, _ = env
    assert c.post("/api/v1/agents/b/claim", headers=tokens["a"], json={"protocol_version": "1.0"}).status_code == 403
    assert c.post("/api/v1/agents/a/claim", headers=tokens["a"], json={"protocol_version": "2.0"}).status_code == 409


def test_unknown_not_replayed_or_unblocked(env):
    submit(env)
    task = claim(env).json()["task"]
    finish(env, task, "unknown")
    submit(env, key="request-002")
    assert claim(env).json()["task"] is None


def test_restart_does_not_replay_claim(env):
    c, h, tokens, path = env
    task_id = submit(env).json()["id"]
    claim(env)
    restarted = TestClient(create_app(str(path), ADMIN))
    assert restarted.get(f"/api/v1/tasks/{task_id}", headers=h).json()["state"] == "claimed"
    assert restarted.post("/api/v1/agents/a/claim", headers=tokens["a"], json={"protocol_version": "1.0"}).json()["task"] is None


def test_audit_contains_no_agent_token(env):
    c, h, tokens, _ = env
    submit(env)
    events = c.get("/api/v1/audit", headers=h)
    assert "task.submitted" in events.text
    for headers in tokens.values():
        assert headers["Authorization"].split(" ")[1] not in events.text
    assert "token_hash" not in c.get("/api/v1/assets", headers=h).text


def test_audit_failure_prevents_task_commit(env):
    _, _, _, path = env
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE audit")
    with pytest.raises(sqlite3.OperationalError):
        submit(env)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_concurrent_claim_only_once(env):
    submit(env)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: claim(env).json()["task"], range(4)))
    assert sum(x is not None for x in responses) == 1
