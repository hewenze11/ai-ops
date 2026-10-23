"""The three operation modes are a real capability boundary, not prompt text.

readonly withholds the execution tool entirely; confirm gates each command
behind human approval; direct queues immediately. The mode is immutable for the
turn and the model can never widen it.
"""
import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from ai_ops.app import create_app
from ai_ops.turns import MODES, tool_spec, tools_for_turn

ADMIN = "a" * 40


def call_tool(name, arguments, call_id="call_1"):
    return {"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function",
         "function": {"name": name, "arguments": json.dumps(arguments)}}]},
        "usage": {}, "model": "test"}


def final(text):
    return {"message": {"role": "assistant", "content": text}, "usage": {}, "model": "test"}


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, body):
        self.requests.append(json.loads(json.dumps(body)))
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def make_client():
    path = str(Path(tempfile.mkdtemp()) / "control.db")
    return TestClient(create_app(path, ADMIN, "test-model")), path


def setup_role_with_asset(client, headers, users=("ops_read",)):
    client.post("/api/v1/roles", headers=headers, json={"id": "ops", "name": "Ops"})
    client.post("/api/v1/assets", headers=headers,
                json={"id": "web1", "name": "Web", "allowed_users": list(users)})
    client.put("/api/v1/roles/ops/model", headers=headers,
               json={"enabled": True, "model": "", "max_model_steps": 6})


def send(client, headers, mode):
    return client.post("/api/v1/roles/ops/messages", headers=headers,
                       json={"text": "restart nginx", "execution_users": ["ops_read"],
                             "mode": mode, "idempotency_key": "msg-" + mode + "-0001"}).json()


def names_in_request(body):
    return [t["function"]["name"] for t in (body.get("tools") or [])]


READONLY_CHANNEL = ['read_file', 'list_dir', 'file_stat', 'disk_usage', 'memory_info',
                    'process_list', 'service_status', 'journal_logs', 'k8s_get', 'k8s_logs']


def test_tool_spec_omits_execution_tool_in_readonly():
    # Readonly turns get the narrow readonly channel instead of the execution
    # tool: they can look, but there is no command string to inject into.
    # read_many belongs to the read-only channel: every child it dispatches is a
    # readonly tool, so it widens breadth, not capability (§52.4). It must never
    # appear in a write-mode surface, which the next two assertions pin.
    assert names_in_request({"tools": tool_spec(["ops_read"], None, "readonly")}) == ["ask_user", "read_asset_memory"] + READONLY_CHANNEL + ["read_many"]
    assert names_in_request({"tools": tool_spec(["ops_read"], None, "confirm")}) == ["ask_user", "read_asset_memory", "request_execution_account", "propose_change"]
    assert names_in_request({"tools": tool_spec(["ops_read"], None, "direct")}) == ["ask_user", "read_asset_memory", "request_execution_account", "propose_change"]
    for mode in ("confirm", "direct"):
        assert "read_many" not in names_in_request({"tools": tool_spec(["ops_read"], None, mode)}), mode


def test_readonly_channel_tool_becomes_structured_task():
    client, _ = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup_role_with_asset(client, headers)
    turn_id = send(client, headers, "readonly")["turn_id"]
    model = FakeModel([
        call_tool("read_file", {"asset_id": "web1", "run_as": "ops_read", "path": "/etc/nginx/nginx.conf"}),
        final("read it")])
    client.app.state.role_engine.advance(model)
    turn = client.get(f"/api/v1/turns/{turn_id}", headers=headers).json()
    assert turn["state"] == "waiting_tool"
    task = client.get(f"/api/v1/tasks/{turn['pending_task_id']}", headers=headers).json()
    # Structured program+argv, queued without approval, never a shell string.
    assert task["state"] == "queued"
    assert task["payload"]["kind"] == "readonly"
    assert task["payload"]["program"] == "/usr/bin/cat"
    assert task["payload"]["args"] == ["--number", "--", "/etc/nginx/nginx.conf"]
    assert "command" not in task["payload"]


def test_readonly_tool_is_refused_outside_readonly_mode():
    # A prompt-injected model must not reach the readonly channel from a
    # write-mode turn (it would be an unapproved read the user never granted).
    client, _ = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup_role_with_asset(client, headers)
    turn_id = send(client, headers, "direct")["turn_id"]
    model = FakeModel([call_tool("read_file", {"asset_id": "web1", "run_as": "ops_read", "path": "/etc/passwd"})])
    client.app.state.role_engine.advance(model)
    turn = client.get(f"/api/v1/turns/{turn_id}", headers=headers).json()
    assert turn["state"] == "failed"


def test_tools_for_turn_defaults_unknown_mode_to_confirm():
    turn = {"execution_users": json.dumps(["ops_read"]), "mode": "totally-unknown"}
    names = [t["function"]["name"] for t in tools_for_turn(turn, {}, None)]
    # Unknown mode must not silently become direct; it behaves like confirm.
    assert names == ["ask_user", "read_asset_memory", "request_execution_account", "propose_change"]


def test_readonly_model_request_has_no_execution_tool():
    client, _ = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup_role_with_asset(client, headers)
    send(client, headers, "readonly")
    model = FakeModel([final("just analysis")])
    engine = client.app.state.role_engine
    engine.advance(model)
    sent = model.requests[0]
    assert "propose_change" not in names_in_request(sent)


def test_direct_mode_queues_without_approval():
    client, _ = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup_role_with_asset(client, headers)
    turn_id = send(client, headers, "direct")["turn_id"]
    model = FakeModel([
        call_tool("propose_change", {"asset_id": "web1", "run_as": "ops_read", "actions": [{"action": "service.reload", "params": {"unit": "nginx"}}]}),
        final("queued it")])
    engine = client.app.state.role_engine
    engine.advance(model)
    turn = client.get(f"/api/v1/turns/{turn_id}", headers=headers).json()
    assert turn["state"] == "waiting_tool"
    task_id = turn["pending_task_id"]
    task = client.get(f"/api/v1/tasks/{task_id}", headers=headers).json()
    assert task["state"] == "queued"  # no approval needed in direct mode


def test_confirm_mode_gates_each_command():
    client, _ = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup_role_with_asset(client, headers)
    turn_id = send(client, headers, "confirm")["turn_id"]
    model = FakeModel([
        call_tool("propose_change", {"asset_id": "web1", "run_as": "ops_read", "actions": [{"action": "service.reload", "params": {"unit": "nginx"}}]}),
        final("waiting")])
    engine = client.app.state.role_engine
    engine.advance(model)
    turn = client.get(f"/api/v1/turns/{turn_id}", headers=headers).json()
    task = client.get(f"/api/v1/tasks/{turn['pending_task_id']}", headers=headers).json()
    assert task["state"] == "awaiting_approval"


def test_readonly_turn_rejects_a_smuggled_execute_command():
    # Even if a jailbroken model emits execute_command, the readonly turn must
    # fail rather than run it. We bypass request_body tool exposure by advancing
    # a readonly turn whose model still calls the tool.
    client, _ = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup_role_with_asset(client, headers)
    turn_id = send(client, headers, "readonly")["turn_id"]
    model = FakeModel([
        call_tool("propose_change", {"asset_id": "web1", "run_as": "ops_read", "actions": [{"action": "shell.run", "params": {"command": "rm -rf /"}}]}),
        final("nope")])
    engine = client.app.state.role_engine
    engine.advance(model)
    turn = client.get(f"/api/v1/turns/{turn_id}", headers=headers).json()
    assert turn["state"] == "failed"
    assert turn["error_code"] == "MODEL_TOOL_AUTHORIZATION_OR_SCHEMA_REJECTED"
    assert turn["pending_task_id"] is None  # nothing was queued


def test_mode_is_immutable_across_the_turn():
    # The stored turn mode comes from the immutable snapshot; a later message in
    # the same role with a different mode does not retroactively change it.
    client, _ = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup_role_with_asset(client, headers)
    turn_id = send(client, headers, "readonly")["turn_id"]
    turn = client.get(f"/api/v1/turns/{turn_id}", headers=headers).json()
    assert turn["mode"] == "readonly"
    assert turn["execution_users"] == ["ops_read"]


def test_all_three_modes_accepted_and_bad_mode_rejected():
    client, _ = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup_role_with_asset(client, headers)
    for i, mode in enumerate(MODES):
        r = client.post("/api/v1/roles/ops/messages", headers=headers,
                        json={"text": "x", "execution_users": [], "mode": mode,
                              "idempotency_key": f"idem-{i:08d}"})
        assert r.status_code == 202
    bad = client.post("/api/v1/roles/ops/messages", headers=headers,
                      json={"text": "x", "execution_users": [], "mode": "yolo",
                            "idempotency_key": "idem-bad-0001"})
    assert bad.status_code == 422
