"""Stage-one real API/SQLite contracts. No host command or paid model call."""
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from ai_ops.app import create_app

ADMIN = 'stage-one-local-test-admin-01234567890123456789'
GA = {'asset_id': 'asset-a', 'username': 'root'}
GB = {'asset_id': 'asset-b', 'username': 'root'}

class Model:
    def __init__(self, name=None, args=None):
        self.name, self.args, self.calls = name, args, 0
    def complete(self, body):
        self.calls += 1
        if self.name is None:
            return {'message': {'role': 'assistant', 'content': 'done'}}
        return {'message': {'role': 'assistant', 'content': 'requested operation', 'tool_calls': [
            {'id': 'call-one', 'type': 'function', 'function': {'name': self.name, 'arguments': json.dumps(self.args)}}]}}

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_OPS_LOCAL_CONNECTOR_ENABLED', '0')
    app = create_app(str(tmp_path / 'stage.db'), ADMIN, default_model='local-fixture')
    c = TestClient(app, headers={'Authorization': 'Bearer ' + ADMIN})
    assert c.post('/api/v1/roles', json={'id': 'ops', 'name': 'ops'}).status_code == 201
    assert c.put('/api/v1/roles/ops/model', json={'enabled': True}).status_code == 200
    tokens = {}
    for aid in ('asset-a', 'asset-b'):
        r = c.post('/api/v1/assets', json={'id': aid, 'name': aid, 'allowed_users': ['root']})
        assert r.status_code == 201
        tokens[aid] = r.json()['agent_token']
    return app, c, tokens

def send(env, grants=None, key='message-first-key', **extra):
    r = env[1].post('/api/v1/roles/ops/messages', json={'text': 'inspect only', 'execution_grants': grants or [], 'idempotency_key': key, **extra})
    assert r.status_code == 202, r.text
    return r.json()['turn_id']

def turn(env, tid):
    return env[1].get('/api/v1/turns/' + tid).json()

def request(env, username='root', grants=None):
    tid = send(env, grants)
    model = Model('request_execution_account', {'asset_id': 'asset-b', 'username': username, 'reason': 'Need this exact asset account'})
    assert env[0].state.role_engine.advance(model)
    t = turn(env, tid)
    assert t['state'] == 'waiting_user'
    for _ in range(3):
        assert not env[0].state.role_engine.advance(model)
    assert model.calls == 1 and turn(env, tid)['model_steps'] == 1
    req = t['account_requests'][0]
    return tid, req, f"/api/v1/turns/{tid}/account-requests/{req['id']}/answer"

def command(asset='asset-a'):
    return Model('execute_command', {'asset_id': asset, 'run_as': 'root', 'command': 'id', 'timeout_seconds': 10})

def test_same_name_cross_asset_tool_and_legacy_ambiguity(env):
    c = env[1]
    bad = c.post('/api/v1/roles/ops/messages', json={'text': 'x', 'execution_users': ['root'], 'idempotency_key': 'ambiguous-root-key'})
    assert bad.status_code == 422
    tid = send(env, [GA])
    assert env[0].state.role_engine.advance(command('asset-b'))
    t = turn(env, tid)
    assert t['state'] == 'failed' and t['execution_grants'] == [GA]
    assert c.get('/api/v1/console/tasks').json() == []
    assert c.post('/api/v1/assets', json={'name': 'duplicate', 'allowed_users': ['root', 'root']}).status_code == 422


def test_claim_last_mile_revalidates_exact_asset_and_registered_user(env):
    app, c, tokens = env
    tid = send(env, [GA], mode='direct')
    assert app.state.role_engine.advance(command())
    task_id = turn(env, tid)['pending_task_id']
    # Simulate corrupt/stale queued target, not an exposed mutation API.
    with app.state.transaction() as db:
        r = db.execute('SELECT payload FROM tasks WHERE id=?', (task_id,)).fetchone()
        payload = json.loads(r['payload']); payload['asset_id'] = 'asset-b'
        db.execute('UPDATE tasks SET asset_id=?,payload=? WHERE id=?', ('asset-b', json.dumps(payload), task_id))
        from ai_ops.authority import claim_allowed
        assert not claim_allowed(db, db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
    r = c.post('/api/v1/agents/asset-b/claim', headers={'Authorization': 'Bearer ' + tokens['asset-b']}, json={'protocol_version': '1.2'})
    assert r.status_code == 403
    assert c.get('/api/v1/tasks/' + task_id).json()['state'] == 'queued'


def test_approval_audit_snapshot_command_revision_and_confirm_gate(env):
    tid, req, url = request(env, grants=[GA])
    c = env[1]
    assert c.post(url, headers={'Authorization': 'Bearer wrong'}, json={'decision': 'approve', 'revision': 0}).status_code == 403
    assert c.post(url, json={'decision': 'approve', 'revision': 8}).status_code == 409
    assert c.post(url, json={'decision': 'approve', 'revision': 0, 'actor': 'fake-user'}).status_code == 422
    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(lambda _: c.post(url, json={'decision': 'approve', 'revision': 0}), range(2)))
    assert all(r.status_code == 200 for r in replies)
    assert sorted(r.json()['duplicate'] for r in replies) == [False, True]
    t = turn(env, tid)
    assert t['initial_execution_grants'] == [GA] and t['execution_grants'] == [GA, GB]
    assert t['authorization_revision'] == 1 and len(t['authorization_events']) == 1
    event = t['authorization_events'][0]
    assert event['actor'] == 'admin-token-domain' and event['session_ref'] == 'web'
    assert event['reason'] == req['reason'] and event['created_at'] > 0
    assert c.post(url, json={'decision': 'reject', 'revision': 0}).status_code == 409
    assert env[0].state.role_engine.advance(command('asset-b'))
    task = c.get('/api/v1/tasks/' + turn(env, tid)['pending_task_id']).json()
    assert task['state'] == 'awaiting_approval'  # Account grant is NOT command approval.
    assert task['payload']['authorization_revision'] == 1 and task['payload']['run_as'] == 'root'
    assert task['payload']['execution_grants'] == [GA, GB]
    c.post('/api/v1/turns/' + tid + '/cancel')
    assert c.post(url, json={'decision': 'approve', 'revision': 0}).json()['state'] == 'cancelled'
    later = send(env, key='fresh-turn-no-grants')
    assert turn(env, later)['execution_grants'] == []


def test_rejection_continues_without_authority(env):
    tid, req, url = request(env)
    assert env[1].post(url, json={'decision': 'reject', 'revision': 0}).status_code == 200
    t = turn(env, tid)
    assert t['state'] == 'ready' and t['execution_grants'] == [] and t['authorization_events'] == []
    assert env[0].state.role_engine.advance(command('asset-b'))
    assert turn(env, tid)['state'] == 'failed'
    assert env[1].get('/api/v1/console/tasks').json() == []

@pytest.mark.parametrize('terminal', ['cancelled', 'failed', 'completed'])
def test_terminal_request_cannot_resume(env, terminal):
    tid, req, url = request(env)
    if terminal == 'cancelled':
        assert env[1].post('/api/v1/turns/' + tid + '/cancel').status_code == 200
    else:
        with env[0].state.transaction() as db:
            from ai_ops.turns import set_turn_state
            set_turn_state(db, tid, terminal)
    assert env[1].post(url, json={'decision': 'approve', 'revision': 0}).status_code == 409
    assert turn(env, tid)['execution_grants'] == []
    assert turn(env, tid)['account_requests'][0]['state'] == 'cancelled'


def test_missing_account_request_requires_separate_registration(env):
    tid, req, url = request(env, username='missing')
    assert env[1].post(url, json={'decision': 'approve', 'revision': 0}).status_code == 403
    assert turn(env, tid)['state'] == 'waiting_user'
    assert env[1].put('/api/v1/assets/asset-b/notes', json={'allowed_users': ['root', 'missing']}).status_code == 200
    assert turn(env, tid)['execution_grants'] == []  # Registration is not authorization.
    assert env[1].post(url, json={'decision': 'approve', 'revision': 0}).status_code == 200
    assert turn(env, tid)['execution_grants'] == [{'asset_id': 'asset-b', 'username': 'missing'}]


def test_normal_question_does_not_authorize_and_legacy_rows_fail_closed(env):
    tid = send(env)
    model = Model('ask_user', {'question': 'Use B.root?', 'options': [{'id': 'yes', 'label': 'Grant root'}]})
    assert env[0].state.role_engine.advance(model)
    q = turn(env, tid)['questions'][0]
    assert env[1].post(f"/api/v1/turns/{tid}/questions/{q['id']}/answer", json={'option_id': 'yes'}).status_code == 200
    assert turn(env, tid)['execution_grants'] == []
    assert env[0].state.role_engine.advance(command('asset-b'))
    assert turn(env, tid)['state'] == 'failed'
    legacy = send(env, [GA], key='legacy-snapshot-loss', mode='direct')
    with env[0].state.transaction() as db:
        db.execute('DELETE FROM turn_authority WHERE turn_id=?', (legacy,))
    assert env[0].state.role_engine.advance(command())
    assert turn(env, legacy)['state'] == 'failed'


def definition(name, scheduled=False):
    return {'id': name, 'name': name, 'kind': 'scheduled' if scheduled else 'trigger', 'role_id': 'ops', 'prompt': 'inspect', 'execution_grants': [GA], 'mode': 'confirm', **({'cron': '* * * * *'} if scheduled else {})}


def test_trigger_rotation_revocation_isolation_dedupe_and_schedule(env):
    app, c, _ = env
    cfg = definition('job-a', True)
    first = c.post('/api/v1/custom-tasks', json=cfg)
    assert first.status_code == 201
    old = first.json()['trigger_token']
    other = c.post('/api/v1/custom-tasks', json=definition('job-b')).json()['trigger_token']
    def invoke(token, event='event-one', payload=None, extra=None):
        return c.post('/api/v1/triggers/job-a/invoke', headers={'Authorization': 'Bearer ' + token, 'X-Event-ID': event, **(extra or {})}, json=payload or {'alert': 'one'})
    assert invoke(other).status_code == 403
    assert invoke(ADMIN).status_code == 403  # No admin-token fallback for external events.
    accepted = invoke(old)
    assert accepted.status_code == 202
    assert invoke(old).json()['duplicate'] is True
    assert invoke(old, payload={'alert': 'different'}).status_code == 409
    issued = c.post('/api/v1/custom-tasks/job-a/trigger-token')
    assert issued.status_code == 200
    new = issued.json()['trigger_token']; assert new != old
    assert invoke(old).status_code == 403
    assert invoke(new, 'event-two').status_code == 202
    before = c.get('/api/v1/custom-tasks/job-a').json()['next_fire_at']
    assert c.delete('/api/v1/custom-tasks/job-a/trigger-token').status_code == 200
    assert c.delete('/api/v1/custom-tasks/job-a/trigger-token').status_code == 200
    assert invoke(new, 'event-two').status_code == 403  # Auth before old-event dedupe.
    assert c.get('/api/v1/custom-tasks/job-a').json()['next_fire_at'] == before
    assert turn(env, accepted.json()['turn_id'])['state'] == 'queued'
    with app.state.transaction() as db:
        db.execute('UPDATE custom_tasks SET next_fire_at=? WHERE id=?', (time.time(), 'job-a'))
    assert app.state.custom_tasks.materialize() == 1
    def sender(custom_id, event_id, payload):
        r = c.post(f'/api/v1/triggers/{custom_id}/invoke', headers={'X-Schedule-Event-ID': event_id}, json=payload)
        assert r.status_code == 202, r.text
        return r.json()
    assert app.state.custom_tasks.deliver_pending(sender) == 1
    assert invoke(new, extra={'X-Schedule-Event-ID': 'forged'}).status_code == 403
    replacement = c.post('/api/v1/custom-tasks/job-a/trigger-token').json()['trigger_token']
    cfg['enabled'] = False
    assert c.put('/api/v1/custom-tasks/job-a', json=cfg).status_code == 200
    assert invoke(replacement, 'disabled-new').status_code == 409
    assert c.delete('/api/v1/custom-tasks/job-a').status_code == 200
    assert invoke(replacement, 'deleted-new').status_code == 409
    assert c.post('/api/v1/custom-tasks/job-a/trigger-token').status_code == 404
    text = c.get('/api/v1/audit?limit=500').text + c.get('/api/v1/custom-tasks').text
    assert all(secret not in text for secret in (old, new, replacement, other))
    with app.state.read_transaction() as db:
        stored = db.execute('SELECT token_hash FROM custom_tasks WHERE id=?', ('job-a',)).fetchone()[0]
        assert stored == hashlib.sha256(replacement.encode()).hexdigest()
