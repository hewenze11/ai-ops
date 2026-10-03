from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

from fastapi.testclient import TestClient
import pytest

from ai_ops.app import create_app
from ai_ops.model_client import ModelFailure, OpenAICompatible, normalize_response

ADMIN = "role-engine-test-admin-not-real-1234567890"


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, body):
        self.requests.append(body)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def final(text="Verified result"):
    return {"message": {"role": "assistant", "content": text}, "usage": {"total_tokens": 10}}


def tool(user="reader", **extra):
    args = {"asset_id": "host", "run_as": user, "command": "id -un", "timeout_seconds": 30, **extra}
    return {"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call-test", "type": "function", "function": {"name": "execute_command", "arguments": json.dumps(args)}}]}, "usage": {"total_tokens": 10}}


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "turns.db"
    app = create_app(str(path), ADMIN, "test-model")
    c = TestClient(app)
    h = {"Authorization": "Bearer " + ADMIN}
    for name in ("ops", "other"):
        assert c.post("/api/v1/roles", headers=h, json={"id": name, "name": name}).status_code == 201
        assert c.put(f"/api/v1/roles/{name}/model", headers=h, json={"enabled": True}).status_code == 200
    asset = c.post("/api/v1/assets", headers=h, json={"id": "host", "name": "Host", "allowed_users": ["reader", "operator"]}).json()
    agent = {"Authorization": "Bearer " + asset["agent_token"]}
    return c, h, app.state.role_engine, agent, path


def send(env, role="ops", key="message-001", **kwargs):
    c, h, _, _, _ = env
    return c.post(f"/api/v1/roles/{role}/messages", headers=h, json={"text": "Inspect host", "execution_users": ["reader"], "mode": "direct", "idempotency_key": key, **kwargs})


def claim(env):
    return env[0].post("/api/v1/agents/host/claim", headers=env[3], json={"protocol_version": "1.0"}).json()["task"]


def complete(env, task, status="succeeded"):
    return env[0].post(f"/api/v1/agents/host/tasks/{task['id']}/result", headers=env[3], json={"claim_id": task["claim_id"], "status": status, "exit_code": 0 if status == "succeeded" else None, "stdout": "reader\n"})


def get(env, turn):
    return env[0].get("/api/v1/turns/" + turn, headers=env[1]).json()


def test_model_tool_roundtrip_and_audit(env):
    turn = send(env).json()["turn_id"]
    model = FakeModel([tool(), final("Executed as reader")])
    assert env[2].advance(model)
    task = claim(env)
    assert task["execution_users"] == ["reader"] and task["run_as"] == "reader"
    complete(env, task)
    assert env[2].advance(model)
    assert get(env, turn)["state"] == "completed"
    assert get(env, turn)["final_text"] == "Executed as reader"
    calls = env[0].get(f"/api/v1/turns/{turn}/model-calls", headers=env[1]).json()
    assert len(calls) == 2 and calls[1]["request"]["messages"][-1]["role"] == "tool"
    assert ADMIN not in json.dumps(calls) and env[3]["Authorization"].split()[1] not in json.dumps(calls)


def test_entire_multistep_turn_precedes_later_manual_command(env):
    c, h, engine, _, _ = env
    first = send(env).json()["turn_id"]
    later = c.post("/api/v1/tasks", headers=h, json={"role_id": "ops", "asset_id": "host", "execution_users": ["operator"], "run_as": "operator", "command": "echo later", "idempotency_key": "later-manual"}).json()
    model = FakeModel([tool(), tool(command="uname -s"), final()])
    engine.advance(model)
    first_command = claim(env)
    assert first_command["id"] != later["id"]
    complete(env, first_command)
    assert claim(env) is None
    engine.advance(model)
    second_command = claim(env)
    assert second_command["command"] == "uname -s"
    complete(env, second_command)
    engine.advance(model)
    assert get(env, first)["state"] == "completed"
    assert claim(env)["id"] == later["id"]


def test_earlier_manual_blocks_model(env):
    c, h, engine, _, _ = env
    c.post("/api/v1/tasks", headers=h, json={"role_id": "ops", "asset_id": "host", "execution_users": ["reader"], "run_as": "reader", "command": "id", "idempotency_key": "earlier-manual"})
    send(env)
    model = FakeModel([final()])
    assert not engine.advance(model) and not model.requests
    complete(env, claim(env))
    assert engine.advance(model)


def test_confirmation_is_owned_by_turn_not_model(env):
    turn = send(env, mode="confirm").json()["turn_id"]
    model = FakeModel([tool(), final()])
    env[2].advance(model)
    assert claim(env) is None
    task_id = get(env, turn)["pending_task_id"]
    env[0].post(f"/api/v1/tasks/{task_id}/approve", headers=env[1])
    task = claim(env)
    complete(env, task)
    env[2].advance(model)
    assert get(env, turn)["state"] == "completed"


@pytest.mark.parametrize("response", [tool(user="root"), tool(mode="direct"), tool(execution_users=["root"]), tool(asset_id="invented-host")])
def test_model_cannot_expand_authority(env, response):
    turn = send(env).json()["turn_id"]
    env[2].advance(FakeModel([response]))
    assert get(env, turn)["error_code"] == "MODEL_TOOL_AUTHORIZATION_OR_SCHEMA_REJECTED"
    assert claim(env) is None


def test_future_message_cannot_replace_current_accounts(env):
    send(env)
    send(env, key="message-002", execution_users=["operator"])
    model = FakeModel([tool()])
    env[2].advance(model)
    task = claim(env)
    assert task["execution_users"] == ["reader"]


def test_unknown_execution_blocks_following_turns(env):
    first = send(env).json()["turn_id"]
    send(env, key="message-002")
    model = FakeModel([tool(), final()])
    env[2].advance(model)
    complete(env, claim(env), status="unknown")
    assert get(env, first)["state"] == "blocked_unknown"
    assert not env[2].advance(model)
    assert len(model.requests) == 1


def test_disabled_role_has_no_paid_model_call(env):
    env[0].put("/api/v1/roles/ops/model", headers=env[1], json={"enabled": False})
    send(env)
    model = FakeModel([])
    assert not env[2].advance(model) and not model.requests


def test_messages_idempotent_and_conflict(env):
    a = send(env)
    b = send(env)
    assert a.json()["turn_id"] == b.json()["turn_id"]
    assert send(env, text="different").status_code == 409


def test_same_role_cannot_make_concurrent_model_calls(env):
    send(env)
    send(env, key="message-002")
    entered, release = threading.Event(), threading.Event()
    class Blocking:
        def complete(self, body):
            entered.set()
            assert release.wait(5)
            return final()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(env[2].advance, Blocking())
        assert entered.wait(5)
        assert not env[2].advance(FakeModel([]))
        release.set()
        assert first.result(timeout=5)


def test_different_role_can_progress_while_one_model_waits(env):
    send(env)
    send(env, role="other")
    entered, release = threading.Event(), threading.Event()
    class Blocking:
        def complete(self, body):
            entered.set()
            assert release.wait(5)
            return final()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(env[2].advance, Blocking())
        assert entered.wait(5)
        assert env[2].advance(FakeModel([final("Other role")]))
        release.set()
        first.result(timeout=5)


def test_full_documents_and_api_rebuilt_after_tool(env):
    c, h, engine, _, _ = env
    c.put("/api/v1/documents/core", headers=h, json={"id": "core", "name": "Core", "content": "CORE_MARKER_VERSION_ONE", "core": True})
    c.put("/api/v1/documents/private", headers=h, json={"id": "private", "name": "Private", "content": "OTHER_ROLE_ONLY_MARKER", "core": False, "role_ids": ["other"]})
    send(env)
    model = FakeModel([tool(), final()])
    engine.advance(model)
    complete(env, claim(env))
    c.put("/api/v1/documents/core", headers=h, json={"id": "core", "name": "Core", "content": "CORE_MARKER_VERSION_TWO", "core": True})
    engine.advance(model)
    a, b = [r["messages"][0]["content"] for r in model.requests]
    assert "CORE_MARKER_VERSION_ONE" in a and "CORE_MARKER_VERSION_TWO" in b
    assert "OTHER_ROLE_ONLY_MARKER" not in a and "OTHER_ROLE_ONLY_MARKER" not in b
    for path in c.app.openapi()["paths"]:
        assert path in a and path in b


def test_role_is_told_which_product_it_belongs_to_and_its_capabilities(env):
    c, h, engine, _, _ = env
    send(env)
    model = FakeModel([final()])
    engine.advance(model)
    system = model.requests[0]["messages"][0]["content"]
    # Product identity + capability map are a mandatory, always-present tier.
    assert "SYSTEM_SELF_DESCRIPTION" in system
    assert "AI Ops" in system
    for capability in ("assets", "memory", "custom tasks", "alarms", "channels", "tasks & approval"):
        assert capability.lower() in system.lower()
    # And it must be told the API surface is the PRODUCT's, not its own tool.
    assert "NOT your tool" in system


def test_self_description_does_not_grant_anything_to_readonly(env):
    c, h, engine, _, _ = env
    # readonly: seeing the capability map must not hand the model any tool.
    send(env, mode="readonly")
    model = FakeModel([final()])
    engine.advance(model)
    tools = model.requests[0].get("tools") or []
    names = [t["function"]["name"] for t in tools]
    assert "execute_command" not in names
    assert "SYSTEM_SELF_DESCRIPTION" in model.requests[0]["messages"][0]["content"]


def test_role_history_isolated(env):
    send(env, text="PRIVATE_OPS_MARKER")
    env[2].advance(FakeModel([final("PRIVATE_OPS_REPLY")]))
    send(env, role="other", text="unrelated")
    model = FakeModel([final()])
    env[2].advance(model)
    assert "PRIVATE_OPS_MARKER" not in json.dumps(model.requests)
    assert "PRIVATE_OPS_REPLY" not in json.dumps(model.requests)


def test_context_limit_fails_instead_of_truncating_core(env):
    env[0].put("/api/v1/roles/ops/model", headers=env[1], json={"enabled": True, "max_context_chars": 1000})
    turn = send(env).json()["turn_id"]
    model = FakeModel([])
    env[2].advance(model)
    assert get(env, turn)["error_code"] == "MANDATORY_CONTEXT_TOO_LARGE" and not model.requests


def test_model_failure_does_not_create_remote_command(env):
    turn = send(env).json()["turn_id"]
    env[2].advance(FakeModel([ModelFailure("MODEL_HTTP_429")]))
    assert get(env, turn)["state"] == "failed" and claim(env) is None


def test_model_audit_failure_prevents_network_call(env):
    send(env)
    with sqlite3.connect(env[4]) as db:
        db.execute("DROP TABLE audit")
    model = FakeModel([])
    with pytest.raises(sqlite3.OperationalError):
        env[2].advance(model)
    assert not model.requests


def test_step_budget_stops_without_new_call(env):
    env[0].put("/api/v1/roles/ops/model", headers=env[1], json={"enabled": True, "max_model_steps": 1})
    turn = send(env).json()["turn_id"]
    model = FakeModel([tool()])
    env[2].advance(model)
    complete(env, claim(env))
    env[2].advance(model)
    assert get(env, turn)["error_code"] == "MODEL_STEP_BUDGET_EXHAUSTED"
    assert len(model.requests) == 1


def test_restart_during_model_request_is_not_replayed(env):
    turn = send(env).json()["turn_id"]
    with sqlite3.connect(env[4]) as db:
        db.execute("UPDATE role_turns SET state='calling' WHERE id=?", (turn,))
    restarted = TestClient(create_app(str(env[4]), ADMIN, "test-model"))
    assert restarted.get("/api/v1/turns/" + turn, headers=env[1]).json()["error_code"] == "MODEL_CALL_INTERRUPTED"


def test_trigger_input_uses_same_turn_queue(env):
    c, h, engine, _, _ = env
    cfg = {"id": "alert", "name": "Alert", "kind": "trigger", "role_id": "ops", "prompt": "Explain this event", "execution_users": ["reader"], "mode": "direct"}
    token = c.post("/api/v1/custom-tasks", headers=h, json=cfg).json()["trigger_token"]
    event = c.post("/api/v1/triggers/alert/invoke", headers={"Authorization": "Bearer " + token}, json={"alert": "test"}).json()
    later = send(env).json()["turn_id"]
    engine.advance(FakeModel([final("Event explained")]))
    assert get(env, event["turn_id"])["state"] == "completed"
    assert get(env, later)["state"] == "queued"
    assert c.get("/api/v1/custom-task-events", headers=h).json()[0]["state"] == "completed"


def test_cancel_waiting_approval_turn(env):
    turn = send(env, mode="confirm").json()["turn_id"]
    env[2].advance(FakeModel([tool()]))
    assert env[0].post(f"/api/v1/turns/{turn}/cancel", headers=env[1]).status_code == 200
    assert claim(env) is None
    assert get(env, turn)["state"] == "cancelled"


def test_transport_requires_https_without_embedded_credentials(tmp_path):
    for base in ("http://example.com/v1", "https://user:secret@example.com/v1", "https://example.com/v1?secret=x"):
        with pytest.raises(ValueError):
            OpenAICompatible(base, tmp_path / "unused")


def test_normalizer_excludes_hidden_reasoning_and_rejects_truncation():
    body = {"model": "test", "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Answer", "reasoning_content": "not requested"}}], "usage": {"total_tokens": 20}}
    result = normalize_response(body)
    assert "reasoning" not in json.dumps(result)
    body["choices"][0]["finish_reason"] = "length"
    with pytest.raises(ModelFailure):
        normalize_response(body)


def test_same_day_prior_turn_is_replayed_for_continuity(env):
    """A role must remember its earlier same-day turns.

    The age-tiered daily memory intentionally excludes TODAY, so without an
    explicit same-day replay a role would start every turn with amnesia about
    what it just did — the exact "it doesn't hang together" failure. Here we run
    one completed turn, then start a second and assert the first turn's prompt
    and final text are replayed ahead of the new user message.
    """
    c, h, engine, _, _ = env
    first = send(env, key="continuity-001").json()["turn_id"]
    engine.advance(FakeModel([final("FIRST_ANSWER_MARKER")]))
    assert get(env, first)["state"] == "completed"

    second = send(env, key="continuity-002").json()["turn_id"]
    model = FakeModel([final("second")])
    engine.advance(model)
    contents = [m["content"] for m in model.requests[0]["messages"]]
    # The earlier same-day turn is replayed as a user/assistant pair.
    assert "FIRST_ANSWER_MARKER" in contents
    assert any(m["role"] == "assistant" and m["content"] == "FIRST_ANSWER_MARKER"
               for m in model.requests[0]["messages"])
    # The current turn's own prompt is still the last user message.
    assert model.requests[0]["messages"][-1]["role"] == "user"
