"""Derived execution modes: fork a built-in, change only its list bindings.

The security claim these tests exist to defend is the one that would be easiest
to break by accident: a fork must never be a quieter route to more authority. A
mode row stores its capability in ``base_behavior`` and every gate reads *that*,
so naming a fork "放行模式 2" cannot give it the ``direct`` behaviour.
"""
import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from ai_ops.app import create_app
from ai_ops import modes as modes_module

ADMIN = "a" * 40


def make_client():
    path = str(Path(tempfile.mkdtemp()) / "control.db")
    return TestClient(create_app(path, ADMIN, "test-model")), path


def h():
    return {"Authorization": "Bearer " + ADMIN}


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, body):
        self.requests.append(json.loads(json.dumps(body)))
        return self.responses.pop(0)


def final(text):
    return {"message": {"role": "assistant", "content": text}, "usage": {}, "model": "t"}


def tool_names(body):
    return [t["function"]["name"] for t in (body.get("tools") or [])]


def test_three_builtin_modes_exist_and_are_not_deletable():
    client, _ = make_client()
    modes = client.get("/api/v1/modes", headers=h()).json()
    by_id = {m["id"]: m for m in modes}
    assert {"readonly", "confirm", "direct"} <= set(by_id)
    for mode_id in ("readonly", "confirm", "direct"):
        assert by_id[mode_id]["builtin"] is True
        assert client.delete("/api/v1/modes/" + mode_id, headers=h()).status_code == 409


def test_forking_a_builtin_inherits_its_behaviour():
    client, _ = make_client()
    created = client.post("/api/v1/modes/confirm/fork", headers=h(),
                          json={"name": "夜班审计"}).json()
    assert created["base_mode"] == "confirm"
    assert created["base_behavior"] == "confirm"
    assert created["builtin"] is False


def test_a_fork_cannot_be_forked_again():
    client, _ = make_client()
    first = client.post("/api/v1/modes/direct/fork", headers=h(), json={"name": "一级"}).json()
    second = client.post("/api/v1/modes/%s/fork" % first["id"], headers=h(), json={"name": "二级"})
    assert second.status_code == 409


def test_a_fork_of_readonly_gets_no_change_tool_even_named_as_pass_through():
    # The dangerous mistake: assume a fork is a "fresh mode" and let its name or
    # some default decide its power. It must behave exactly like its parent.
    client, _ = make_client()
    fork = client.post("/api/v1/modes/readonly/fork", headers=h(),
                       json={"name": "放行模式副本"}).json()
    client.post("/api/v1/roles", headers=h(), json={"id": "ops", "name": "Ops"})
    client.post("/api/v1/assets", headers=h(),
                json={"id": "web1", "name": "Web", "allowed_users": ["ops_read"]})
    client.put("/api/v1/roles/ops/model", headers=h(), json={"enabled": True, "model": ""})
    client.post("/api/v1/roles/ops/messages", headers=h(),
                json={"text": "go", "execution_users": ["ops_read"], "mode": fork["id"],
                      "idempotency_key": "fork-check-0001"})
    model = FakeModel([final("ok")])
    client.app.state.role_engine.advance(model)
    names = tool_names(model.requests[0])
    assert "propose_change" not in names
    assert "disk_usage" in names


def test_a_fork_keeps_its_parent_behaviour_in_the_database():
    client, path = make_client()
    fork = client.post("/api/v1/modes/direct/fork", headers=h(), json={"name": "快车道"}).json()
    import sqlite3
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT base_behavior,base_mode FROM execution_modes WHERE id=?",
                         (fork["id"],)).fetchone()
    assert row == ("direct", "direct")


def test_fork_can_rebind_list_versions():
    client, _ = make_client()
    lists = client.get("/api/v1/name-lists", headers=h()).json()
    write_list = next(x["id"] for x in lists if x["kind"] == "write")
    fork = client.post("/api/v1/modes/confirm/fork", headers=h(),
                       json={"name": "绑定测试", "write_list": write_list}).json()
    assert fork["write_list"] == write_list
    assert fork["base_behavior"] == "confirm"


def test_fork_inherits_parent_bindings_when_none_are_given():
    client, _ = make_client()
    parent = client.get("/api/v1/modes/confirm", headers=h()).json()
    fork = client.post("/api/v1/modes/confirm/fork", headers=h(), json={"name": "继承测试"}).json()
    assert fork["write_list"] == parent["write_list"]
    assert fork["readonly_list"] == parent["readonly_list"]


def test_updating_a_fork_cannot_change_its_behaviour():
    client, path = make_client()
    fork = client.post("/api/v1/modes/readonly/fork", headers=h(), json={"name": "只读副本"}).json()
    # 'base_behavior' is not an accepted field, so a client cannot set it, and
    # even a successful update leaves the stored behaviour untouched.
    response = client.put("/api/v1/modes/" + fork["id"], headers=h(),
                          json={"name": "改名了", "base_behavior": "direct"})
    assert response.status_code == 422
    response = client.put("/api/v1/modes/" + fork["id"], headers=h(), json={"name": "改名了"})
    assert response.status_code == 200
    assert response.json()["base_behavior"] == "readonly"


def test_builtin_modes_cannot_be_edited():
    client, _ = make_client()
    response = client.put("/api/v1/modes/confirm", headers=h(), json={"name": "改了"})
    assert response.status_code == 409


def test_duplicate_mode_name_is_rejected():
    client, _ = make_client()
    client.post("/api/v1/modes/confirm/fork", headers=h(), json={"name": "重名"})
    again = client.post("/api/v1/modes/direct/fork", headers=h(), json={"name": "重名"})
    assert again.status_code == 409


def test_unknown_mode_resolves_to_readonly_not_direct():
    # A typo or a missing row must not hand out a write surface.
    client, path = make_client()
    import sqlite3
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        assert modes_module.behavior_of(db, "no-such-mode") == "readonly"
        assert modes_module.behavior_of(db, "direct") == "direct"


def test_deleting_a_fork_in_use_is_refused():
    client, _ = make_client()
    fork = client.post("/api/v1/modes/confirm/fork", headers=h(), json={"name": "在用模式"}).json()
    client.post("/api/v1/roles", headers=h(), json={"id": "ops", "name": "Ops"})
    client.post("/api/v1/assets", headers=h(),
                json={"id": "web1", "name": "Web", "allowed_users": ["ops_read"]})
    client.put("/api/v1/roles/ops/model", headers=h(), json={"enabled": True, "model": ""})
    client.post("/api/v1/roles/ops/messages", headers=h(),
                json={"text": "hi", "execution_users": ["ops_read"], "mode": fork["id"],
                      "idempotency_key": "inuse-check-0001"})
    response = client.delete("/api/v1/modes/" + fork["id"], headers=h())
    assert response.status_code == 409


def test_deleting_an_unused_fork_works():
    client, _ = make_client()
    fork = client.post("/api/v1/modes/confirm/fork", headers=h(), json={"name": "临时"}).json()
    assert client.delete("/api/v1/modes/" + fork["id"], headers=h()).status_code == 200
    assert client.get("/api/v1/modes/" + fork["id"], headers=h()).status_code == 404
