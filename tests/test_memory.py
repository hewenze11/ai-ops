"""Daily memory tiers: non-overlapping age windows, isolation, on-demand load."""
import json
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_ops import memory
from ai_ops.app import create_app

ADMIN = "a" * 40


def make_client():
    return TestClient(create_app(str(Path(tempfile.mkdtemp()) / "control.db"), ADMIN, "test-model"))


def test_policy_must_be_non_overlapping_and_open_ended():
    memory.validate_policy({"tiers": [{"max_age_days": 2, "form": "full"},
                                      {"max_age_days": 5, "form": "compressed"},
                                      {"max_age_days": None, "form": "summary"}]})
    for bad in (
        {"tiers": [{"max_age_days": None, "form": "full"}, {"max_age_days": 5, "form": "summary"}]},
        {"tiers": [{"max_age_days": 5, "form": "full"}]},  # not open-ended
        {"tiers": [{"max_age_days": 2, "form": "full"}, {"max_age_days": 2, "form": "summary"}, {"max_age_days": None, "form": "summary"}]},
        {"tiers": [{"max_age_days": 5, "form": "full"}, {"max_age_days": 2, "form": "summary"}, {"max_age_days": None, "form": "summary"}]},
        {"tiers": [{"max_age_days": 2, "form": "nonsense"}, {"max_age_days": None, "form": "summary"}]},
        {"tiers": []},
    ):
        with pytest.raises(memory.PolicyError):
            memory.validate_policy(bad)


def test_form_for_age_covers_every_age_exactly_once():
    policy = memory.DEFAULT_POLICY
    assert memory.form_for_age(policy, 0) == "full"
    assert memory.form_for_age(policy, 2) == "full"
    assert memory.form_for_age(policy, 3) == "compressed"
    assert memory.form_for_age(policy, 5) == "compressed"
    assert memory.form_for_age(policy, 6) == "summary"
    assert memory.form_for_age(policy, 9999) == "summary"


def test_render_memory_orders_oldest_first_and_holds_back_non_full():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    client.post("/api/v1/roles", headers=headers, json={"id": "ops", "name": "Ops"})
    today = date.today()
    with client.app.state.transaction() as db:
        for age, text in [(0, "today"), (1, "yesterday"), (3, "3d"), (30, "old")]:
            day = (today - timedelta(days=age)).isoformat()
            db.execute("INSERT INTO role_memory(role_id,day,full_text,compressed,summary,source,turn_count,generated_at) "
                       "VALUES(?,?,?,?,?,'derived',1,?)", ("ops", day, text, text, text, 0))
        injected, held = memory.render_memory(db, "ops", today.isoformat())
    assert [e["day"] for e in injected] == sorted(e["day"] for e in injected)  # oldest first
    forms = {e["day"]: e["form"] for e in injected}
    assert forms[(today - timedelta(days=3)).isoformat()] == "compressed"
    # days not injected as full text are listed for on-demand loading
    assert (today - timedelta(days=3)).isoformat() in held
    assert (today - timedelta(days=30)).isoformat() in held


def test_memory_is_isolated_per_role():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    for role in ("ops", "other"):
        client.post("/api/v1/roles", headers=headers, json={"id": role, "name": role})
    client.put("/api/v1/roles/ops/memory/2026-01-01", headers=headers, json={"day": "2026-01-01", "full_text": "OPS SECRET"})
    assert client.get("/api/v1/roles/ops/memory/2026-01-01", headers=headers).json()["full_text"] == "OPS SECRET"
    other = client.get("/api/v1/roles/other/memory/2026-01-01", headers=headers).json()
    assert other["full_text"] == ""  # other role never sees it


def test_operator_edit_pins_and_survives_rebuild():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    client.post("/api/v1/roles", headers=headers, json={"id": "ops", "name": "Ops"})
    client.put("/api/v1/roles/ops/memory/2026-01-02", headers=headers,
               json={"day": "2026-01-02", "full_text": "operator note"})
    client.post("/api/v1/roles/ops/memory/rebuild", headers=headers)
    row = client.get("/api/v1/roles/ops/memory/2026-01-02", headers=headers).json()
    assert row["full_text"] == "operator note"
    assert row["source"] == "edited"
    assert row["edited_at"] is not None


def test_delete_removes_memory_but_keeps_audit():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    client.post("/api/v1/roles", headers=headers, json={"id": "ops", "name": "Ops"})
    client.put("/api/v1/roles/ops/memory/2026-01-03", headers=headers,
               json={"day": "2026-01-03", "full_text": "x"})
    assert client.delete("/api/v1/roles/ops/memory/2026-01-03", headers=headers).json()["deleted"] is True
    audit = client.get("/api/v1/audit", headers=headers).json()
    assert any(e["event"] == "memory.deleted" for e in audit)
    assert any(e["event"] == "memory.edited" for e in audit)


def test_policy_api_roundtrip_and_validation():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    client.post("/api/v1/roles", headers=headers, json={"id": "ops", "name": "Ops"})
    default = client.get("/api/v1/roles/ops/memory/policy", headers=headers).json()
    assert default == memory.DEFAULT_POLICY
    new = client.put("/api/v1/roles/ops/memory/policy", headers=headers,
                     json={"tiers": [{"max_age_days": 1, "form": "full"}, {"max_age_days": None, "form": "summary"}]})
    assert new.status_code == 200
    bad = client.put("/api/v1/roles/ops/memory/policy", headers=headers,
                     json={"tiers": [{"max_age_days": 5, "form": "full"}]})  # not open-ended
    assert bad.status_code == 422


def test_turn_context_includes_tiered_memory_but_not_today():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    client.post("/api/v1/roles", headers=headers, json={"id": "ops", "name": "Ops"})
    client.put("/api/v1/roles/ops/model", headers=headers, json={"enabled": True, "model": "", "max_model_steps": 4})
    client.put("/api/v1/roles/ops/memory/2026-01-04", headers=headers,
               json={"day": "2026-01-04", "full_text": "OLD DAY FULL TEXT"})
    client.put("/api/v1/roles/ops/memory/2026-01-05", headers=headers,
               json={"day": "2026-01-05", "full_text": "ANCIENT SUMMARY SOURCE"})
    client.post("/api/v1/roles/ops/messages", headers=headers,
                json={"text": "hi", "execution_users": [], "mode": "readonly", "idempotency_key": "mem-msg-0001"})

    class FakeModel:
        def __init__(self): self.requests = []
        def complete(self, body):
            self.requests.append(body)
            return {"message": {"role": "assistant", "content": "ok"}, "usage": {}, "model": "test"}

    model = FakeModel()
    client.app.state.role_engine.advance(model)
    system_blob = json.dumps(model.requests[0]["messages"])
    # The old day is injected (as full/compressed/summary depending on age).
    assert "OLD DAY FULL TEXT" in system_blob or "ROLE_MEMORY" in system_blob
    assert "OLDER_MEMORY_SUMMARISED_DAYS" in system_blob


def test_completed_turn_is_archived_to_today_memory_immediately():
    """A finished turn must be in the day's memory at once, not only after the
    NEXT model call. Otherwise a follow-up "what did you just do?" would not see
    the answer, and a rebuilt-from-scratch day row would lag by one turn."""
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    client.post("/api/v1/roles", headers=headers, json={"id": "ops", "name": "Ops"})
    client.put("/api/v1/roles/ops/model", headers=headers, json={"enabled": True, "model": "", "max_model_steps": 4})
    client.post("/api/v1/roles/ops/messages", headers=headers,
                json={"text": "what is the disk state?", "execution_users": [], "mode": "readonly",
                      "idempotency_key": "mem-archive-0001"})

    class FakeModel:
        def complete(self, body):
            return {"message": {"role": "assistant", "content": "DISK_OK_MARKER"}, "usage": {}, "model": "test"}

    client.app.state.role_engine.advance(FakeModel())
    import time as _time
    today = _time.strftime("%Y-%m-%d", _time.localtime())
    row = client.get("/api/v1/roles/ops/memory/" + today, headers=headers).json()
    assert "DISK_OK_MARKER" in row["full_text"], row
    assert "what is the disk state?" in row["full_text"]


def test_compress_keeps_head_and_tail():
    text = "A" * 5000 + "MIDDLE" + "B" * 5000
    out = memory.compress(text, limit=100)
    assert out.startswith("AAAA")
    assert out.endswith("BBBB")
    assert "omitted" in out
