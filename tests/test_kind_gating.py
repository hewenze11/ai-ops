"""Protocol 1.3 kind gating: an old Agent must never receive readonly tasks."""
import json

from test_control import env, ADMIN


def make_readonly_task(c, h, key="ro-001"):
    """Submit a readonly task through the same path the model tool uses."""
    with c.app.state.transaction() as db:
        # Reuse a real turn so grants and ordering behave like production.
        from ai_ops.turns import enqueue_turn
        asset = db.execute("SELECT * FROM assets WHERE id='a'").fetchone()
        turn_id = enqueue_turn(db, 'ops', 'command', 'pending', ['reader'], 'readonly',
                               'readonly probe', {}, None)
        db.execute("UPDATE role_turns SET state='waiting_tool' WHERE id=?", (turn_id,))
        # Real turns always carry explicit grants; emulate that so the claim path
        # exercises the same authority check production uses.
        db.execute("INSERT OR REPLACE INTO turn_authority(turn_id,grants,initial_grants,revision) VALUES(?,?,?,0)",
                   (turn_id, json.dumps([{'asset_id': 'a', 'username': 'reader'}]), json.dumps([{'asset_id': 'a', 'username': 'reader'}])))
        task_id = 'ro-task-' + key
        payload = {'kind': 'readonly', 'tool': 'read_file', 'program': '/usr/bin/cat',
                   'args': ['--number', '--', '/etc/hostname'], 'asset_id': 'a',
                   'run_as': 'reader', 'execution_users': ['reader'], 'timeout_seconds': 60,
                   'role_id': 'ops', 'mode': 'readonly', 'idempotency_key': 'ro:' + key}
        db.execute("INSERT INTO tasks(id,role_id,asset_id,payload,state,idempotency_key,created_at,updated_at,turn_id) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (task_id, 'ops', 'a', json.dumps(payload), 'queued', payload['idempotency_key'], 1.0, 1.0, turn_id))
        db.commit()
    return task_id


def test_old_agent_does_not_receive_readonly_task(env):
    c, h, tokens, path = env
    task_id = make_readonly_task(c, h)
    # A 1.2 agent is fully compatible for shell work but cannot read `kind`.
    response = c.post("/api/v1/agents/a/claim", headers=tokens["a"],
                      json={"protocol_version": "1.2"}).json()
    assert response["task"] is None
    assert response.get("blocked_kind") is True
    assert task_id in response["blocked_task_ids"]
    # The task stays queued for a 1.3 agent rather than being dropped.
    assert c.get(f"/api/v1/tasks/{task_id}", headers=h).json()["state"] == "queued"


def test_1_3_agent_receives_the_same_task(env):
    c, h, tokens, path = env
    task_id = make_readonly_task(c, h)
    response = c.post("/api/v1/agents/a/claim", headers=tokens["a"],
                      json={"protocol_version": "1.3"}).json()
    assert response["task"]["id"] == task_id
    assert response["task"]["kind"] == "readonly"
    assert response["task"]["program"] == "/usr/bin/cat"


def test_blocked_kind_is_audited_once_not_per_poll(env):
    c, h, tokens, path = env
    make_readonly_task(c, h)
    for _ in range(3):
        c.post("/api/v1/agents/a/claim", headers=tokens["a"], json={"protocol_version": "1.2"})
    rows = c.get("/api/v1/audit", headers=h).json()
    items = rows["items"] if isinstance(rows, dict) else rows
    assert len([r for r in items if r["event"] == "task.kind_blocked"]) == 1
