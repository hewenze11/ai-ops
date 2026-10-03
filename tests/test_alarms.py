"""Built-in alarm log: every alarm is recorded by default, multiple sources can
share one trigger path, and the log is durable and independent of the turn."""
import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from ai_ops.app import create_app
from ai_ops import alarms

ADMIN = "a" * 40


def make_client():
    return TestClient(create_app(str(Path(tempfile.mkdtemp()) / "control.db"), ADMIN, "test-model"))


def setup(client, headers):
    client.post("/api/v1/roles", headers=headers, json={"id": "ops", "name": "Ops"})
    created = client.post("/api/v1/custom-tasks", headers=headers, json={
        "id": "alerts", "name": "Alerts", "kind": "trigger", "role_id": "ops",
        "prompt": "诊断告警", "execution_users": ["ops_read"], "mode": "confirm", "enabled": True})
    return created.json()["trigger_token"]


def test_alarm_entry_extracts_common_fields():
    entry = alarms.alarm_entry({"host": "web1", "severity": "critical", "title": "Disk full",
                                "message": "/var at 98%"})
    assert entry["source"] == "web1"
    assert entry["severity"] == "critical"
    assert entry["title"] == "Disk full"
    assert entry["summary"] == "/var at 98%"


def test_alarm_entry_falls_back_to_payload_summary():
    entry = alarms.alarm_entry({"weird": "shape"})
    assert entry["source"] == "unknown"
    assert "weird" in entry["summary"]


def test_every_accepted_alarm_is_logged_by_default():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    token = setup(client, headers)
    r = client.post("/api/v1/triggers/alerts/invoke", headers={"Authorization": "Bearer " + token},
                    json={"host": "web1", "severity": "warning", "title": "High load"})
    assert r.status_code == 202
    log = client.get("/api/v1/alarms", headers=headers).json()
    assert len(log) == 1
    assert log[0]["state"] == "accepted"
    assert log[0]["source"] == "web1"
    assert log[0]["custom_task_id"] == "alerts"
    assert log[0]["turn_id"] == r.json()["turn_id"]


def test_multiple_sources_share_one_trigger_and_are_separable():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    token = setup(client, headers)
    for host in ("web1", "web2", "db1"):
        client.post("/api/v1/triggers/alerts/invoke",
                    headers={"Authorization": "Bearer " + token, "X-Alarm-Source": host},
                    json={"host": host, "title": "alert from " + host})
    log = client.get("/api/v1/alarms", headers=headers).json()
    assert {row["source"] for row in log} == {"web1", "web2", "db1"}
    only_web1 = client.get("/api/v1/alarms?source=web1", headers=headers).json()
    assert len(only_web1) == 1 and only_web1[0]["source"] == "web1"
    sources = client.get("/api/v1/alarms/sources", headers=headers).json()
    assert len(sources) == 3


def test_alarm_source_header_overrides_payload_and_is_validated():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    token = setup(client, headers)
    client.post("/api/v1/triggers/alerts/invoke",
                headers={"Authorization": "Bearer " + token, "X-Alarm-Source": "monitor-A"},
                json={"host": "ignored", "title": "x"})
    log = client.get("/api/v1/alarms", headers=headers).json()
    assert log[0]["source"] == "monitor-A"
    bad = client.post("/api/v1/triggers/alerts/invoke",
                      headers={"Authorization": "Bearer " + token, "X-Alarm-Source": "x" * 300},
                      json={"title": "x"})
    assert bad.status_code == 422


def test_duplicate_alarm_is_logged_not_silently_dropped():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    token = setup(client, headers)
    body = {"host": "web1", "title": "same"}
    h = {"Authorization": "Bearer " + token, "X-Event-ID": "evt-1"}
    first = client.post("/api/v1/triggers/alerts/invoke", headers=h, json=body).json()
    second = client.post("/api/v1/triggers/alerts/invoke", headers=h, json=body).json()
    assert second["duplicate"] is True and second["event_id"] == first["event_id"]
    log = client.get("/api/v1/alarms", headers=headers).json()
    assert [row["state"] for row in log] == ["accepted", "duplicate"]


def test_rejected_alarm_is_still_logged():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    token = setup(client, headers)
    client.put("/api/v1/custom-tasks/alerts", headers=headers, json={
        "id": "alerts", "name": "Alerts", "kind": "trigger", "role_id": "ops",
        "prompt": "诊断告警", "execution_users": ["ops_read"], "mode": "confirm", "enabled": False})
    r = client.post("/api/v1/triggers/alerts/invoke", headers={"Authorization": "Bearer " + token},
                    json={"host": "web1", "title": "late"})
    assert r.status_code == 409
    log = client.get("/api/v1/alarms", headers=headers).json()
    assert len(log) == 1 and log[0]["state"] == "rejected"
    assert log[0]["detail"] == "disabled_or_deleted"


def test_alarm_log_redacts_secrets_in_payload():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    token = setup(client, headers)
    client.post("/api/v1/triggers/alerts/invoke", headers={"Authorization": "Bearer " + token},
                json={"host": "web1", "password": "hunter2hunter2", "title": "cred"})
    log = client.get("/api/v1/alarms", headers=headers).json()
    assert "hunter2hunter2" not in json.dumps(log[0]["payload"])
    assert "[REDACTED]" in json.dumps(log[0]["payload"])


def test_alarm_survives_turn_failure_and_is_queryable_per_task():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    token = setup(client, headers)
    client.post("/api/v1/triggers/alerts/invoke", headers={"Authorization": "Bearer " + token},
                json={"host": "web1", "title": "one"})
    # Even if the role/queue is later disturbed, the alarm row remains.
    per_task = client.get("/api/v1/alarms?custom_task_id=alerts", headers=headers).json()
    assert len(per_task) == 1
    single = client.get(f"/api/v1/alarms/{per_task[0]['id']}", headers=headers).json()
    assert single["payload"]["title"] == "one"
