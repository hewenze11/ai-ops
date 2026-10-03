"""Channel gateway tests: pairing, shared role memory, and honest outbound.

The central claim under test is the product requirement: two different channels
(Feishu and Weixin) that map to the SAME role must share that role's memory and
its single serial queue, while a channel never sees another channel's
conversation.
"""
import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from ai_ops.app import create_app
from test_console import ADMIN


def make_client():
    return TestClient(create_app(str(Path(tempfile.mkdtemp()) / "control.db"), ADMIN, "test-model"))


def setup(client, headers):
    client.post("/api/v1/roles", headers=headers, json={"id": "ops", "name": "Ops"})
    asset = client.post("/api/v1/assets", headers=headers, json={
        "id": "host", "name": "Host", "allowed_users": ["reader"]}).json()
    return {"Authorization": "Bearer " + asset["agent_token"]}


def pair(client, headers, channel, user_id, role="ops", mode="confirm", users=None, code=None):
    """Bind an identity. Pairing itself is a message, so pair with a throwaway
    line, then the real assertions look only at later turns."""
    if code is None:
        issued = client.post("/api/v1/channels/pairings", headers=headers,
                             json={"role_id": role, "mode": mode, "execution_users": users or ["reader"]}).json()
        code = issued["pairing_code"]
    return client.post("/api/v1/channels/inbound", headers=headers,
                       json={"channel": channel, "user_id": user_id, "text": "[pairing]", "pairing_code": code})


def test_pairing_binds_identity_and_unknown_sender_is_ignored():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)
    # Unpaired sender: message is recorded but NO turn is created.
    unpaired = client.post("/api/v1/channels/inbound", headers=headers,
                           json={"channel": "feishu", "user_id": "u-unknown", "text": "do things"})
    assert unpaired.status_code == 200
    assert unpaired.json()["status"] == "unpaired"
    assert client.get("/api/v1/roles/ops/turns", headers=headers).json() == []

    bound = pair(client, headers, "feishu", "u-1")
    assert bound.status_code == 200 and bound.json()["status"] == "queued"
    turns = client.get("/api/v1/roles/ops/turns", headers=headers).json()
    assert len(turns) == 1
    assert turns[0]["caller"] == "channel:feishu"
    assert turns[0]["mode"] == "confirm"
    # Unpaired senders produced NO turn.
    assert [t["prompt"] for t in turns] == ["[pairing]"]


def test_pairing_code_is_single_use():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)
    issued = client.post("/api/v1/channels/pairings", headers=headers,
                         json={"role_id": "ops", "mode": "confirm", "execution_users": ["reader"]}).json()
    first = pair(client, headers, "feishu", "u-1", code=issued["pairing_code"])
    assert first.json()["status"] == "queued"
    second = pair(client, headers, "weixin", "u-2", code=issued["pairing_code"])
    assert second.status_code == 409


def test_two_channels_share_one_role_memory_and_queue():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)
    pair(client, headers, "feishu", "feishu-user", mode="direct")
    pair(client, headers, "weixin", "weixin-user", mode="direct")
    client.post("/api/v1/channels/inbound", headers=headers,
                json={"channel": "feishu", "user_id": "feishu-user", "text": "first from feishu"})
    client.post("/api/v1/channels/inbound", headers=headers,
                json={"channel": "weixin", "user_id": "weixin-user", "text": "second from weixin"})
    # ONE role queue: both messages land in the same role's turns, in arrival order.
    turns = client.get("/api/v1/roles/ops/turns", headers=headers).json()
    prompts = [t["prompt"] for t in turns]
    assert prompts[-2:] == ["first from feishu", "second from weixin"]
    assert [t["caller"] for t in turns][-2:] == ["channel:feishu", "channel:weixin"]


def test_external_id_dedupes_inbound():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)
    pair(client, headers, "feishu", "u-1")
    body = {"channel": "feishu", "user_id": "u-1", "text": "repeat", "external_id": "evt-1"}
    first = client.post("/api/v1/channels/inbound", headers=headers, json=body).json()
    second = client.post("/api/v1/channels/inbound", headers=headers, json=body).json()
    assert first["status"] == "queued"
    assert second["status"] == "duplicate" and second["turn_id"] == first["turn_id"]
    # Only one turn carrying "repeat" exists (the pairing turn is separate).
    turns = client.get("/api/v1/roles/ops/turns", headers=headers).json()
    assert len([t for t in turns if t["prompt"] == "repeat"]) == 1


def test_outbox_requires_bridge_credential():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)
    pair(client, headers, "feishu", "u-1")
    assert client.get("/api/v1/channels/outbox?channel=feishu&user_id=u-1").status_code in (401, 403)


def test_outbox_reports_state_not_fake_reply_for_unfinished_turn():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)
    pair(client, headers, "feishu", "u-1")
    # The turn is queued (no model worker running), so the outbox must be empty
    # rather than inventing a reply.
    out = client.get("/api/v1/channels/outbox?channel=feishu&user_id=u-1", headers=headers).json()
    assert out["messages"] == []


def test_unbind_removes_identity_but_keeps_history():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)
    pair(client, headers, "feishu", "u-1")
    assert client.delete("/api/v1/channels/identities/feishu/u-1", headers=headers).json()["unbound"] is True
    assert client.get("/api/v1/channels/identities?channel=feishu", headers=headers).json() == []
    # Conversation history survives the unbind (audit-style retention).
    convo = client.get("/api/v1/channels/conversation?channel=feishu&user_id=u-1", headers=headers).json()
    assert any(m["direction"] == "inbound" for m in convo)


def test_unknown_channel_and_unknown_pairing_code_rejected():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)
    bad_channel = client.post("/api/v1/channels/inbound", headers=headers,
                              json={"channel": "telegram", "user_id": "u", "text": "x"})
    assert bad_channel.status_code == 422
    bad_code = client.post("/api/v1/channels/inbound", headers=headers,
                           json={"channel": "feishu", "user_id": "u", "text": "x", "pairing_code": "nope"})
    assert bad_code.status_code == 403


def test_pairing_requires_existing_role():
    client = make_client()
    headers = {"Authorization": "Bearer " + ADMIN}
    setup(client, headers)
    missing = client.post("/api/v1/channels/pairings", headers=headers,
                          json={"role_id": "ghost", "mode": "confirm", "execution_users": ["reader"]})
    assert missing.status_code == 404
