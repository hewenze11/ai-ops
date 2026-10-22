"""Stage 6 backend: the operator inbox, reject-with-reason, credential
annotation and audit filters that the console is built on."""
import json

from test_actions import ADMIN, FakeModel, call_tool, final, make, service


def test_inbox_lists_a_held_change_with_the_bytes_that_would_run():
    client, h, turn_id = make("confirm")
    client.app.state.role_engine.advance(FakeModel([
        call_tool("propose_change", {"asset_id": "web1", "run_as": "ops_read",
                                     "actions": [service("nginx")], "reason": "reload after cert renew"}),
        final("waiting")]))
    box = client.get("/api/v1/inbox", headers=h).json()
    assert box["total"] == 1 and len(box["approvals"]) == 1
    card = box["approvals"][0]
    assert card["turn_id"] == turn_id and card["asset_name"] == "W" and card["role_name"] == "Ops"
    assert card["run_as"] == "ops_read" and card["mode"] == "confirm"
    assert card["steps"][0]["args"] == ["reload", "nginx"]
    assert card["steps"][0]["action"] == "service.reload"
    assert len(card["payload_sha256"]) == 64
    assert card["reason"] == "reload after cert renew"
    assert card["prompt"] == "reload nginx"
    assert box["questions"] == [] and box["account_requests"] == []


def test_reject_returns_the_reason_to_the_model_and_keeps_the_turn_alive():
    client, h, turn_id = make("confirm")
    client.app.state.role_engine.advance(FakeModel([
        call_tool("propose_change", {"asset_id": "web1", "run_as": "ops_read",
                                     "actions": [service("nginx")]}),
        final("ok I will not")]))
    task_id = client.get(f"/api/v1/turns/{turn_id}", headers=h).json()["pending_task_id"]
    r = client.post(f"/api/v1/tasks/{task_id}/reject", headers=h, json={"reason": "not during business hours"})
    assert r.status_code == 200 and r.json() == {"state": "rejected"}
    assert client.get("/api/v1/inbox", headers=h).json()["total"] == 0
    # Second decision on the same task is refused, whichever way it goes.
    assert client.post(f"/api/v1/tasks/{task_id}/approve", headers=h).status_code == 409
    assert client.post(f"/api/v1/tasks/{task_id}/reject", headers=h).status_code == 409
    # The engine resumes the turn with the rejection as the tool output; the
    # model gets to read the operator's reason and continue.
    client.app.state.role_engine.advance(FakeModel([final("ok I will not")]))
    turn = client.get(f"/api/v1/turns/{turn_id}", headers=h).json()
    assert turn["state"] == "completed"
    tool_msg = [m for m in turn["messages"] if m.get("role") == "tool"][-1]["content"]
    body = json.loads(tool_msg)
    assert body["status"] == "rejected" and body["reason"] == "not during business hours"
    assert body["command_approval_granted"] is False
    events = client.get("/api/v1/audit", headers=h, params={"event_prefix": "task.rejected"}).json()
    assert len(events) == 1 and events[0]["details"]["reason"] == "not during business hours"


def test_approve_records_an_optional_operator_note():
    client, h, turn_id = make("confirm")
    client.app.state.role_engine.advance(FakeModel([
        call_tool("propose_change", {"asset_id": "web1", "run_as": "ops_read",
                                     "actions": [service("nginx")]})]))
    task_id = client.get(f"/api/v1/turns/{turn_id}", headers=h).json()["pending_task_id"]
    assert client.post(f"/api/v1/tasks/{task_id}/approve", headers=h, json={"reason": "window open"}).json() == {"state": "queued"}
    events = client.get("/api/v1/audit", headers=h, params={"entity_id": task_id, "event_prefix": "task.approved"}).json()
    assert events[0]["details"] == {"reason": "window open"}


def test_inbox_shows_questions_and_account_requests_side_by_side():
    client, h, turn_id = make("confirm")
    client.app.state.role_engine.advance(FakeModel([
        call_tool("ask_user", {"question": "Which env?", "options": [{"id": "prod", "label": "Prod"}]})]))
    box = client.get("/api/v1/inbox", headers=h).json()
    assert box["total"] == 1 and box["questions"][0]["question"] == "Which env?"
    assert box["questions"][0]["turn_id"] == turn_id
    qid = box["questions"][0]["question_id"]
    client.post(f"/api/v1/turns/{turn_id}/questions/{qid}/answer", headers=h, json={"option_id": "prod"})
    assert client.get("/api/v1/inbox", headers=h).json()["total"] == 0
    # Account request on the same turn.
    client.post("/api/v1/assets", headers=h, json={"id": "db1", "name": "DB", "allowed_users": ["dba"]})
    client.app.state.role_engine.advance(FakeModel([
        call_tool("request_execution_account", {"asset_id": "db1", "username": "dba", "reason": "need to restart db"})]))
    box = client.get("/api/v1/inbox", headers=h).json()
    assert box["total"] == 1
    req = box["account_requests"][0]
    assert req["asset_name"] == "DB" and req["username"] == "dba" and "revision" in req


def test_cancelled_turn_drops_its_question_from_the_inbox():
    client, h, turn_id = make("confirm")
    client.app.state.role_engine.advance(FakeModel([
        call_tool("ask_user", {"question": "Go?", "options": [{"id": "y", "label": "Yes"}]})]))
    assert client.get("/api/v1/inbox", headers=h).json()["total"] == 1
    client.post(f"/api/v1/turns/{turn_id}/cancel", headers=h)
    assert client.get("/api/v1/inbox", headers=h).json()["total"] == 0


def test_credential_handle_can_be_renamed_but_evidence_fields_cannot():
    client, h, _ = make("confirm")
    client.post("/api/v1/assets/web1/credentials", headers=h,
                json={"id": "cred-1", "name": "unnamed-1", "source": "/opt/app/.env", "kind": "pattern"})
    r = client.put("/api/v1/assets/web1/credentials/cred-1", headers=h,
                   json={"name": "prod-mysql-root", "note": "used by app to connect mysql"})
    assert r.status_code == 200
    assert r.json()["name"] == "prod-mysql-root" and r.json()["source"] == "/opt/app/.env"
    # source/kind/id are evidence, not editable.
    assert client.put("/api/v1/assets/web1/credentials/cred-1", headers=h, json={"source": "/x"}).status_code == 422
    assert client.put("/api/v1/assets/web1/credentials/nope", headers=h, json={"name": "x"}).status_code == 404
    history = client.get("/api/v1/assets/web1/credentials/cred-1/history", headers=h).json()
    assert [e["event"] for e in history] == ["discovered", "annotated"]
    # Still no value anywhere in the inventory.
    listing = client.get("/api/v1/credentials", headers=h).json()
    assert "value" not in listing["items"][0] and "secret" not in json.dumps(listing)


def test_audit_filters_and_backward_paging():
    client, h, _ = make("confirm")
    full = client.get("/api/v1/audit", headers=h, params={"limit": 500}).json()
    assert len(full) >= 3
    newest = client.get("/api/v1/audit", headers=h, params={"before": full[-1]["seq"] + 1, "limit": 2}).json()
    assert [e["seq"] for e in newest] == [full[-1]["seq"], full[-2]["seq"]]
    by_actor = client.get("/api/v1/audit", headers=h, params={"actor": "admin"}).json()
    assert by_actor and all(e["actor"] == "admin" for e in by_actor)
    # LIKE metacharacters in the prefix are literal, not wildcards.
    assert client.get("/api/v1/audit", headers=h, params={"event_prefix": "%"}).json() == []
    assert client.get("/api/v1/audit", headers=h, params={"event_prefix": "role"}).json()
