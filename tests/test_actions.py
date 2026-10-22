"""Gate 3/4: structured change sets and the policy engine (§5, §39.1, §43.5, §44)."""
import json

import pytest
from fastapi.testclient import TestClient

from ai_ops import actions
from ai_ops.actions import ChangeRejected, decide, expand, normalize, payload_hash
from ai_ops.app import create_app

ADMIN = "a" * 40


def service(unit="nginx"):
    return {"action": "service.reload", "params": {"unit": unit}}


# --- action table -----------------------------------------------------------

def test_expand_produces_argv_not_a_command_string():
    step = expand("service.reload", {"unit": "nginx"})
    assert step["program"] == "/usr/bin/systemctl"
    assert step["args"] == ["reload", "nginx"]
    assert step["auto_approvable"] is True


def test_unknown_action_and_param_drift_rejected():
    with pytest.raises(ChangeRejected):
        expand("firewall.disable", {})
    with pytest.raises(ChangeRejected):
        expand("service.reload", {"unit": "nginx", "extra": 1})
    with pytest.raises(ChangeRejected):
        expand("service.reload", {})
    with pytest.raises(ChangeRejected):
        expand("service.reload", {"unit": "nginx; rm -rf /"})


def test_baseline_denied_paths_cannot_be_written_even_by_an_allowed_action():
    # A user list is a refinement on top of the baseline, never a way past it.
    with pytest.raises(ChangeRejected):
        expand("file.write", {"path": "/etc/shadow", "content": "x"})
    with pytest.raises(ChangeRejected):
        expand("file.write", {"path": "/etc/sudoers.d/x", "content": "x"})
    with pytest.raises(ChangeRejected):
        expand("file.append", {"path": "/root/.ssh/authorized_keys", "line": "k"})


def test_relative_and_traversal_paths_rejected():
    for path in ("etc/nginx/nginx.conf", "/etc/../etc/passwd", "/etc/./nginx.conf/../x"):
        with pytest.raises(ChangeRejected):
            expand("log.rotate", {"path": path})


def test_change_set_is_bounded_and_ordered():
    steps = normalize([service("nginx"), service("redis")])
    assert [s["params"]["unit"] for s in steps] == ["nginx", "redis"]
    with pytest.raises(ChangeRejected):
        normalize([])
    with pytest.raises(ChangeRejected):
        normalize([service()] * (actions.MAX_ACTIONS + 1))


# --- policy engine ----------------------------------------------------------

def step(action="service.reload", **params):
    return expand(action, params or {"unit": "nginx"})


def test_readonly_mode_can_never_run_a_change():
    assert decide("readonly", step())[0] == "reject"


def test_confirm_mode_always_asks_a_human():
    assert decide("confirm", step())[:2] == ("approve", "CONFIRM")
    assert decide("confirm", step("service.restart", unit="db"))[:2] == ("approve", "CONFIRM")


def test_direct_mode_auto_runs_only_flagged_actions():
    assert decide("direct", step())[:2] == ("execute", "DIRECT_AUTO")
    # Stopping a service is high risk and not flagged: still needs a human.
    assert decide("direct", step("service.stop", unit="db"))[:2] == ("approve", "DIRECT_NOT_AUTO")
    assert decide("direct", step("shell.run", command="id"))[:2] == ("approve", "DIRECT_NOT_AUTO")


def test_trigger_narrows_to_its_configured_actions():
    ctx = {"trigger_source": "alarm", "trigger_actions": ["disk.clean_temp"]}
    assert decide("direct", step("disk.clean_temp", path="/var/tmp", days=7), **ctx)[0] == "execute"
    # Same mode, same auto-approvable action, but not in this trigger's set.
    assert decide("direct", step("service.restart", unit="db"), **ctx) == ("reject", "TRIGGER_ACTION_NOT_PERMITTED")


def test_trigger_cannot_use_an_action_that_needs_a_human():
    ctx = {"trigger_source": "alarm", "trigger_actions": ["shell.run"]}
    assert decide("direct", step("shell.run", command="id"), **ctx) == ("reject", "TRIGGER_ACTION_NEEDS_HUMAN")


def test_trigger_must_be_direct_mode_not_confirm():
    ctx = {"trigger_source": "alarm", "trigger_actions": ["disk.clean_temp"]}
    assert decide("confirm", step("disk.clean_temp", path="/var/tmp", days=7), **ctx) == ("reject", "TRIGGER_REQUIRES_DIRECT_MODE")


def test_host_scope_from_a_rule_is_enforced():
    ctx = {"asset_tags": ["staging"], "action_hosts": {"service.restart": ["prod"]}}
    assert decide("direct", step("service.restart", unit="db"), **ctx) == ("reject", "HOST_OUT_OF_SCOPE")
    ctx["asset_tags"] = ["prod"]
    assert decide("direct", step("service.restart", unit="db"), **ctx)[0] == "execute"


# --- approval binding (§39.3) -----------------------------------------------

def test_hash_covers_the_write_content_not_only_the_path():
    # Regression (found on a real host, 2026-10-22): for file.write the argv is
    # just the path, so a hash over argv alone let a tampered body present as
    # "approved". The content must be part of the binding.
    a = expand("file.write", {"path": "/etc/app.conf", "content": "key=1"})
    b = expand("file.write", {"path": "/etc/app.conf", "content": "key=2"})
    assert payload_hash([a], "web1", "ops") != payload_hash([b], "web1", "ops")
    # Same for shell commands, which live only in params.
    x = expand("shell.run", {"command": "id"})
    y = expand("shell.run", {"command": "reboot"})
    assert payload_hash([x], "web1", "ops") != payload_hash([y], "web1", "ops")


def test_payload_hash_covers_target_account_and_argv():
    steps = [expand("service.reload", {"unit": "nginx"})]
    base = payload_hash(steps, "web1", "ops")
    assert base != payload_hash(steps, "web2", "ops")      # different host
    assert base != payload_hash(steps, "web1", "root")     # different account
    other = [expand("service.reload", {"unit": "redis"})]
    assert base != payload_hash(other, "web1", "ops")      # different argv
    assert base == payload_hash([expand("service.reload", {"unit": "nginx"})], "web1", "ops")


# --- turn integration -------------------------------------------------------

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

    def complete(self, body):
        return self.responses.pop(0)


def make(mode="direct"):
    import tempfile
    from pathlib import Path
    client = TestClient(create_app(str(Path(tempfile.mkdtemp()) / "c.db"), ADMIN, "test-model"))
    h = {"Authorization": "Bearer " + ADMIN}
    client.post("/api/v1/roles", headers=h, json={"id": "ops", "name": "Ops"})
    client.post("/api/v1/assets", headers=h, json={"id": "web1", "name": "W", "allowed_users": ["ops_read"]})
    client.put("/api/v1/roles/ops/model", headers=h, json={"enabled": True, "model": "", "max_model_steps": 6})
    turn = client.post("/api/v1/roles/ops/messages", headers=h,
                       json={"text": "reload nginx", "execution_users": ["ops_read"], "mode": mode,
                             "idempotency_key": "k-" + mode}).json()
    return client, h, turn["turn_id"]


def test_direct_mode_runs_an_auto_approvable_change_without_asking():
    client, h, turn_id = make("direct")
    client.app.state.role_engine.advance(FakeModel([
        call_tool("propose_change", {"asset_id": "web1", "run_as": "ops_read",
                                     "actions": [service("nginx")], "reason": "config reload"}),
        final("done")]))
    turn = client.get(f"/api/v1/turns/{turn_id}", headers=h).json()
    task = client.get(f"/api/v1/tasks/{turn['pending_task_id']}", headers=h).json()
    assert task["state"] == "queued"
    assert task["payload"]["kind"] == "change"
    assert task["payload"]["steps"][0]["args"] == ["reload", "nginx"]
    assert task["payload"]["approval"]["payload_sha256"] == payload_hash(
        task["payload"]["steps"], "web1", "ops_read")


def test_confirm_mode_holds_the_same_change_for_a_human():
    client, h, turn_id = make("confirm")
    client.app.state.role_engine.advance(FakeModel([
        call_tool("propose_change", {"asset_id": "web1", "run_as": "ops_read",
                                     "actions": [service("nginx")]}),
        final("waiting")]))
    turn = client.get(f"/api/v1/turns/{turn_id}", headers=h).json()
    task = client.get(f"/api/v1/tasks/{turn['pending_task_id']}", headers=h).json()
    assert task["state"] == "awaiting_approval"


def test_one_refused_step_refuses_the_whole_set():
    # Atomicity: a prompt cannot smuggle a permitted action alongside a refused
    # one and hope the permitted half runs.
    client, h, turn_id = make("direct")
    client.app.state.role_engine.advance(FakeModel([
        call_tool("propose_change", {"asset_id": "web1", "run_as": "ops_read",
                                     "actions": [service("nginx"),
                                                 {"action": "file.write",
                                                  "params": {"path": "/etc/shadow", "content": "x"}}]}),
        final("declined")]))
    turn = client.get(f"/api/v1/turns/{turn_id}", headers=h).json()
    assert turn["pending_task_id"] is None
    content = [m for m in turn["messages"] if m.get("role") == "tool"][-1]["content"]
    assert "rejected" in content and "PATH_DENIED_BASELINE" in content


def test_model_cannot_smuggle_extra_arguments_into_a_change():
    # `execution_users`/`mode` were never model-selectable; make sure the new
    # tool does not quietly start accepting them either.
    client, h, turn_id = make("direct")
    client.app.state.role_engine.advance(FakeModel([
        call_tool("propose_change", {"asset_id": "web1", "run_as": "ops_read",
                                     "actions": [service("nginx")], "execution_users": ["root"]})]))
    turn = client.get(f"/api/v1/turns/{turn_id}", headers=h).json()
    assert turn["state"] == "failed"
    assert turn["error_code"] == "MODEL_TOOL_AUTHORIZATION_OR_SCHEMA_REJECTED"
