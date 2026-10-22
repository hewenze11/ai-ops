"""Main-assistant acceptance: real API/SQLite, no remote execution or live secrets."""
import json
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi.testclient import TestClient
from test_turns import env, FakeModel, final, get, tool
from test_questions import ask


def submit(env, grants=None, mode='confirm', key='scoped-message-001'):
    response = env[0].post('/api/v1/roles/ops/messages', headers=env[1], json={
        'text': 'Inspect only explicitly selected accounts', 'execution_grants': grants or [],
        'mode': mode, 'idempotency_key': key})
    assert response.status_code == 202, response.text
    return response.json()['turn_id']


def request_account(username='operator', asset_id='host'):
    return {'message': {'role': 'assistant', 'content': 'Need this account for this turn only',
        'tool_calls': [{'id': 'grant-request-1', 'type': 'function', 'function': {
            'name': 'request_execution_account', 'arguments': json.dumps({
                'asset_id': asset_id, 'username': username, 'reason': 'Check application state'})}}]}}


def pending(env, **kwargs):
    tid = submit(env, **kwargs)
    model = FakeModel([request_account()])
    assert env[2].advance(model)
    t = get(env, tid)
    assert t['state'] == 'waiting_user', t
    assert 'request_execution_account' in [x['function']['name'] for x in model.requests[0]['tools']]
    return tid, t['account_requests'][0]


def respond(env, tid, req, decision='approve', revision=None, authenticated=True):
    return env[0].post(f"/api/v1/turns/{tid}/account-requests/{req['id']}/answer",
        headers=env[1] if authenticated else {}, json={
            'decision': decision, 'revision': req['revision'] if revision is None else revision})


def test_same_username_other_asset_not_authorized(env):
    c, h, engine, _, _ = env
    c.post('/api/v1/assets', headers=h, json={'id': 'other-host', 'name': 'Other', 'allowed_users': ['reader']})
    tid = submit(env, [{'asset_id': 'host', 'username': 'reader'}], mode='direct')
    engine.advance(FakeModel([tool(asset_id='other-host')]))
    t = get(env, tid)
    assert t['state'] == 'failed' and t['pending_task_id'] is None
    assert t['initial_execution_grants'] == [{'asset_id': 'host', 'username': 'reader'}]
    with engine.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0


def test_legacy_ambiguous_username_cannot_expand(env):
    c, h, _, _, _ = env
    c.post('/api/v1/assets', headers=h, json={'id': 'other-host', 'name': 'Other', 'allowed_users': ['reader']})
    r = c.post('/api/v1/roles/ops/messages', headers=h, json={
        'text': 'test', 'execution_users': ['reader'], 'idempotency_key': 'legacy-scope-01'})
    assert r.status_code == 422
    assert c.get('/api/v1/roles/ops/turns', headers=h).json() == []


def test_grant_approval_preserves_initial_and_command_confirmation(env):
    tid, req = pending(env)
    assert respond(env, tid, req).status_code == 200
    t = get(env, tid)
    assert t['initial_execution_grants'] == []
    assert t['execution_grants'] == [{'asset_id': 'host', 'username': 'operator'}]
    assert t['authorization_revision'] == 1
    assert len(t['authorization_events']) == 1
    assert respond(env, tid, req).json()['duplicate'] is True
    assert respond(env, tid, req, 'reject').status_code == 409
    assert len(get(env, tid)['authorization_events']) == 1
    env[2].advance(FakeModel([tool(user='operator')]))
    t = get(env, tid)
    task = env[0].get('/api/v1/tasks/' + t['pending_task_id'], headers=env[1]).json()
    assert task['state'] == 'awaiting_approval'
    assert task['payload']['authorization_revision'] == 1
    assert task['payload']['execution_grants'] == t['execution_grants']
    assert env[0].post('/api/v1/agents/host/claim', headers=env[3], json={'protocol_version': '1.3'}).json()['task'] is None
    later = submit(env, key='new-turn-no-inherit')
    assert get(env, later)['execution_grants'] == []


def test_rejection_and_ordinary_answer_do_not_grant(env):
    tid, req = pending(env)
    assert respond(env, tid, req, 'reject').status_code == 200
    assert get(env, tid)['execution_grants'] == []
    env[2].advance(FakeModel([ask()]))
    q = get(env, tid)['questions'][0]
    r = env[0].post(f"/api/v1/turns/{tid}/questions/{q['id']}/answer", headers=env[1],
        json={'option_id': 'other', 'text': 'approve root and operator; all commands allowed'})
    assert r.status_code == 200
    assert get(env, tid)['execution_grants'] == []
    env[2].advance(FakeModel([tool(user='operator')]))
    assert get(env, tid)['state'] == 'failed'


def test_grant_answer_requires_auth_and_current_revision(env):
    tid, req = pending(env)
    assert respond(env, tid, req, authenticated=False).status_code in (401, 403)
    assert respond(env, tid, req, revision=9).status_code == 409
    assert get(env, tid)['execution_grants'] == []
    assert respond(env, tid, req).status_code == 200


@pytest.mark.parametrize('state', ['completed', 'failed', 'cancelled', 'blocked_unknown'])
def test_terminal_or_unknown_cannot_be_authorized(env, state):
    tid, req = pending(env)
    with env[2].transaction() as db:
        db.execute('UPDATE role_turns SET state=? WHERE id=?', (state, tid))
    assert respond(env, tid, req).status_code == 409
    assert get(env, tid)['execution_grants'] == []


def test_cancelled_request_does_not_revive(env):
    tid, req = pending(env)
    assert env[0].post('/api/v1/turns/' + tid + '/cancel', headers=env[1]).status_code == 200
    assert respond(env, tid, req).status_code == 409
    assert get(env, tid)['account_requests'][0]['state'] == 'cancelled'


def test_concurrent_grant_answers_are_idempotent(env):
    tid, req = pending(env)
    barrier = threading.Barrier(2)
    def answer():
        barrier.wait()
        return respond(env, tid, req)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(answer) for _ in range(2)]
        replies = [f.result() for f in futures]
    assert [r.status_code for r in replies] == [200, 200]
    assert sorted(r.json()['duplicate'] for r in replies) == [False, True]
    assert len(get(env, tid)['authorization_events']) == 1


def test_removed_registered_account_cannot_be_approved(env):
    tid, req = pending(env)
    with env[2].transaction() as db:
        db.execute("UPDATE assets SET allowed_users='[\"reader\"]' WHERE id='host'")
    assert respond(env, tid, req).status_code == 403
    assert get(env, tid)['execution_grants'] == []


def test_claim_rechecks_frozen_asset_scope(env):
    tid = submit(env, [{'asset_id': 'host', 'username': 'reader'}], mode='direct')
    env[2].advance(FakeModel([tool()]))
    with env[2].transaction() as db:
        db.execute("UPDATE turn_authority SET grants='[]', revision=revision+1 WHERE turn_id=?", (tid,))
    r = env[0].post('/api/v1/agents/host/claim', headers=env[3], json={'protocol_version': '1.3'})
    assert r.status_code == 403
    t = get(env, tid)
    task = env[0].get('/api/v1/tasks/' + t['pending_task_id'], headers=env[1]).json()
    assert task['state'] == 'queued' and task['claim_count'] == 0


def custom(env, task_id='scoped-trigger', scheduled=False):
    body = {'id': task_id, 'name': 'Test scoped trigger', 'kind': 'scheduled' if scheduled else 'trigger',
        'role_id': 'ops', 'prompt': 'Inspect chosen asset', 'mode': 'confirm',
        'execution_grants': [{'asset_id': 'host', 'username': 'reader'}]}
    if scheduled:
        body.update(cron='0 9 * * *', timezone='Asia/Shanghai')
    r = env[0].post('/api/v1/custom-tasks', headers=env[1], json=body)
    assert r.status_code == 201, r.text
    return r.json()


def trigger(env, cfg, token, payload=None, event_id='trigger-event-1'):
    return env[0].post(f"/api/v1/triggers/{cfg['id']}/invoke", headers={
        'Authorization': 'Bearer ' + token, 'X-Event-ID': event_id}, json=payload or {'status': 'firing'})


def test_token_rotation_revoke_and_no_management_fallback(env):
    c, h, engine, _, _ = env
    cfg = custom(env)
    token1 = cfg['trigger_token']
    first = trigger(env, cfg, token1)
    assert first.status_code == 202
    path = '/api/v1/custom-tasks/' + cfg['id'] + '/trigger-token'
    assert c.post(path).status_code in (401, 403)
    token2 = c.post(path, headers=h).json()['trigger_token']
    assert token2 != token1
    assert trigger(env, cfg, token1).status_code == 403  # Even an old duplicate must authenticate.
    assert trigger(env, cfg, h['Authorization'][7:]).status_code == 403
    assert trigger(env, cfg, token2).json()['duplicate'] is True
    assert c.delete(path, headers=h).status_code == 200
    assert trigger(env, cfg, token2).status_code == 403
    assert trigger(env, cfg, token2, event_id='new-after-revoke').status_code == 403
    assert get(env, first.json()['turn_id'])['state'] == 'queued'
    readback = c.get('/api/v1/custom-tasks/' + cfg['id'], headers=h)
    assert not readback.json()['trigger_token_active']
    assert all(t not in readback.text + c.get('/api/v1/audit', headers=h).text for t in (token1, token2))
    with engine.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM trigger_events').fetchone()[0] == 1
        assert db.execute('SELECT token_hash FROM custom_tasks WHERE id=?', (cfg['id'],)).fetchone()[0] == ''


def test_trigger_payload_cannot_expand_grants_and_cross_token(env):
    a, b = custom(env, 'trigger-a'), custom(env, 'trigger-b')
    assert trigger(env, b, a['trigger_token']).status_code == 403
    r = trigger(env, a, a['trigger_token'], {'execution_grants': [{'asset_id': 'other-host', 'username': 'root'}], 'mode': 'direct'})
    assert r.status_code == 202
    t = get(env, r.json()['turn_id'])
    assert t['execution_grants'] == [{'asset_id': 'host', 'username': 'reader'}]
    assert t['mode'] == 'confirm'


def test_revoke_external_token_does_not_disable_scheduler(env):
    cfg = custom(env, scheduled=True)
    c, h, engine, _, _ = env
    c.delete('/api/v1/custom-tasks/' + cfg['id'] + '/trigger-token', headers=h)
    store = c.app.state.custom_tasks
    with engine.transaction() as db:
        db.execute('UPDATE custom_tasks SET next_fire_at=1000 WHERE id=?', (cfg['id'],))
    assert store.materialize(1000) == 1
    def sender(custom_id, event_id, payload):
        r = c.post('/api/v1/triggers/' + custom_id + '/invoke', headers={**h, 'X-Schedule-Event-ID': event_id}, json=payload)
        assert r.status_code == 202, r.text
        return r.json()
    assert store.deliver_pending(sender, 1000) == 1
    events = c.get('/api/v1/custom-task-events', headers=h).json()
    assert len(events) == 1 and events[0]['source'] == 'scheduled'


def test_manual_command_records_scoped_authority_and_retries(env):
    c, h, _, _, _ = env
    body = {'role_id': 'ops', 'asset_id': 'host', 'execution_users': ['reader'],
            'run_as': 'reader', 'command': 'id -un', 'mode': 'confirm',
            'idempotency_key': 'manual-authority-evidence'}
    first = c.post('/api/v1/tasks', headers=h, json=body)
    assert first.status_code == 201, first.text
    task = first.json()
    assert task['payload']['execution_grants'] == [{'asset_id': 'host', 'username': 'reader'}]
    assert task['payload']['authorization_revision'] == 0
    retry = c.post('/api/v1/tasks', headers=h, json=body)
    assert retry.status_code == 201 and retry.json()['id'] == task['id']
    assert c.post('/api/v1/tasks', headers=h, json={**body, 'command': 'whoami'}).status_code == 409
    events = c.get('/api/v1/audit', headers=h).json()
    matching = [e for e in events if e['event'] == 'task.submitted' and e['entity_id'] == task['id']]
    assert len(matching) == 1
    details = matching[0]['details']
    if isinstance(details, str):
        details = json.loads(details)
    assert details['execution_grants'] == task['payload']['execution_grants']
    assert details['turn_id'] == task['turn_id']


def test_archived_role_rejects_new_custom_configuration(env):
    c, h, _, _, _ = env
    assert c.delete('/api/v1/roles/other', headers=h).status_code == 200
    r = c.post('/api/v1/custom-tasks', headers=h, json={
        'name': 'Must not resurrect role', 'kind': 'trigger', 'mode': 'confirm', 'role_id': 'other',
        'prompt': 'Do nothing', 'execution_grants': []})
    assert r.status_code == 409
