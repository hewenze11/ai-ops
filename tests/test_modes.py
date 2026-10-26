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
    # Read-only tools are available in EVERY mode, because inspecting a host is
    # the prerequisite for proposing a change worth approving. What separates the
    # modes is the write surface: readonly has none, confirm/direct have
    # propose_change. The old shape (readonly tools only in readonly mode) left a
    # confirm turn unable to look at anything, which is how a model ended up
    # retrying forever.
    readonly_surface = ["ask_user", "read_asset_memory"] + READONLY_CHANNEL + ["read_many"]
    assert names_in_request({"tools": tool_spec(["ops_read"], None, "readonly")}) == readonly_surface
    # start_task/check_task travel with propose_change: same authorisation, a
    # different relationship to time (§52.5). A readonly turn never gets them,
    # which the line above pins.
    write_extras = ["request_execution_account", "propose_change", "start_task", "check_task"]
    for mode in ("confirm", "direct"):
        names = names_in_request({"tools": tool_spec(["ops_read"], None, mode)})
        # Same read channel as a readonly turn, plus the write surface. Order is
        # an implementation detail, so compare membership rather than sequences.
        assert set(names) == set(readonly_surface) | set(write_extras), mode
        assert "propose_change" in names, mode
    # Without a selected account there is nothing to propose against, so the
    # write surface stays closed even in a write mode — but reads remain open.
    for mode in ("confirm", "direct", "readonly"):
        names = names_in_request({"tools": tool_spec([], None, mode)})
        assert "propose_change" not in names, mode
        assert "read_file" in names, mode


def test_readonly_channel_never_reaches_the_write_surface():
    # readonly turns must not carry any tool that can change a host.
    names = names_in_request({"tools": tool_spec(["ops_read"], None, "readonly")})
    for forbidden in ("propose_change", "start_task", "check_task", "request_execution_account"):
        assert forbidden not in names


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


def test_readonly_tool_in_write_mode_still_requires_a_granted_account():
    # The real risk the old test was reaching for: a write-mode turn must not
    # become an unapproved read. That is enforced by the per-turn authority
    # check, not by withholding the tool. Here the turn has no granted account,
    # so the read is refused even though the tool is offered.
    client, _ = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup_role_with_asset(client, headers, users=("ops_read",))
    turn_id = client.post("/api/v1/roles/ops/messages", headers=headers,
                          json={"text": "read passwd", "execution_users": [],
                                "mode": "direct", "idempotency_key": "nogrant-0001"}).json()["turn_id"]
    model = FakeModel([call_tool("read_file", {"asset_id": "web1", "run_as": "ops_read", "path": "/etc/passwd"})])
    client.app.state.role_engine.advance(model)
    turn = client.get(f"/api/v1/turns/{turn_id}", headers=headers).json()
    assert turn["state"] == "failed"
    assert turn["error_code"] == "MODEL_TOOL_AUTHORIZATION_OR_SCHEMA_REJECTED"


def test_readonly_tool_in_write_mode_runs_when_account_is_granted():
    # The counterpart, and the bug this pair pins: a confirm turn CAN inspect a
    # host it was granted. Refusing this is what made the model loop.
    client, _ = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup_role_with_asset(client, headers, users=("ops_read",))
    turn_id = client.post("/api/v1/roles/ops/messages", headers=headers,
                          json={"text": "check /tmp", "execution_users": ["ops_read"],
                                "mode": "confirm", "idempotency_key": "granted-0001"}).json()["turn_id"]
    model = FakeModel([call_tool("disk_usage", {"asset_id": "web1", "run_as": "ops_read", "path": "/tmp"}), final("looked")])
    client.app.state.role_engine.advance(model)
    turn = client.get(f"/api/v1/turns/{turn_id}", headers=headers).json()
    assert turn["state"] == "waiting_tool"
    task = client.get(f"/api/v1/tasks/{turn['pending_task_id']}", headers=headers).json()
    assert task["state"] == "queued"
    assert task["payload"]["kind"] == "readonly"
    assert task["payload"]["tool"] == "disk_usage"


def test_tools_for_turn_defaults_unknown_mode_to_readonly():
    turn = {"execution_users": json.dumps(["ops_read"]), "mode": "totally-unknown"}
    names = [t["function"]["name"] for t in tools_for_turn(turn, {}, None)]
    # An unresolvable mode must not hand out a write surface: it gets the
    # read-only channel, never propose_change.
    assert "propose_change" not in names
    assert "disk_usage" in names


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


def test_all_three_modes_accepted_and_unknown_mode_falls_back_to_readonly():
    """Mode ids are data now, so an unknown id is not a schema error.

    A derived mode is a legitimate id, which means the API cannot reject an
    unknown string at the schema layer any more. What matters instead is what it
    resolves to: an id the mode table does not know must never hand out a write
    surface, so it resolves to the read-only behaviour.
    """
    client, db_path = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup_role_with_asset(client, headers)
    for i, mode in enumerate(MODES):
        r = client.post("/api/v1/roles/ops/messages", headers=headers,
                        json={"text": "x", "execution_users": [], "mode": mode,
                              "idempotency_key": f"idem-{i:08d}"})
        assert r.status_code == 202
    unknown = client.post("/api/v1/roles/ops/messages", headers=headers,
                          json={"text": "x", "execution_users": [], "mode": "yolo",
                                "idempotency_key": "idem-bad-0001"})
    assert unknown.status_code == 202
    import sqlite3
    from ai_ops import modes as modes_module
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        assert modes_module.behavior_of(db, "yolo") == "readonly"
