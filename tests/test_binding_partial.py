"""Binding one list slot must not silently unbind the other two.

The endpoint took a raw dict and passed every slot straight through, so a
request that mentioned only `deny_list` wrote `None` into the read and write
lists. The response still looked like a success, so a client had no way to
notice it had just dropped the read-only allowlist for a whole mode.

Now: absent key = leave alone, explicit null = unbind, unknown key = 422.
"""
import pytest
from fastapi.testclient import TestClient

from ai_ops.app import create_app

ADMIN = "admin-token-for-tests-0123456789"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "t.db"), ADMIN)
    with TestClient(app) as c:
        yield c


def headers():
    return {"Authorization": "Bearer " + ADMIN}


def make_list(client, kind, name):
    response = client.post("/api/v1/name-lists", headers=headers(),
                           json={"kind": kind, "name": name, "rules": []})
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def test_setting_one_slot_leaves_the_others_untouched(client):
    ro = make_list(client, "readonly", "ro")
    wr = make_list(client, "write", "wr")
    deny = make_list(client, "deny", "deny")
    client.put("/api/v1/name-list-bindings/confirm", headers=headers(),
               json={"readonly_list": ro, "write_list": wr})
    data = client.put("/api/v1/name-list-bindings/confirm", headers=headers(),
                      json={"deny_list": deny}).json()["confirm"]
    assert data["readonly_list"] == ro
    assert data["write_list"] == wr
    assert data["deny_list"] == deny


def test_an_explicit_null_unbinds_that_slot_only(client):
    ro = make_list(client, "readonly", "ro")
    wr = make_list(client, "write", "wr")
    client.put("/api/v1/name-list-bindings/confirm", headers=headers(),
               json={"readonly_list": ro, "write_list": wr})
    data = client.put("/api/v1/name-list-bindings/confirm", headers=headers(),
                      json={"write_list": None}).json()["confirm"]
    assert data["write_list"] is None
    assert data["readonly_list"] == ro


def test_an_empty_body_changes_nothing(client):
    ro = make_list(client, "readonly", "ro")
    client.put("/api/v1/name-list-bindings/confirm", headers=headers(),
               json={"readonly_list": ro})
    data = client.put("/api/v1/name-list-bindings/confirm", headers=headers(), json={}).json()["confirm"]
    assert data["readonly_list"] == ro


def test_an_unknown_field_is_refused_rather_than_ignored(client):
    response = client.put("/api/v1/name-list-bindings/confirm", headers=headers(),
                          json={"readonly_list": None, "deny_lists": "typo"})
    assert response.status_code == 422
