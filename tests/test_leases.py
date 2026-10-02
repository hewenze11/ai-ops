import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from ai_ops.app import create_app
from test_control import env, submit, claim, finish, ADMIN
from test_execution import newer, api


def test_claim_opens_lease_and_refresh_extends_it(env):
    c, h, _, path = env
    task = newer(env)
    first = c.get('/api/v1/tasks/' + task['id'], headers=h).json()['lease']
    assert first['lease_id'] == task['claim_id'] and first['expired'] is False and first['closed'] is False
    with sqlite3.connect(path) as db:
        db.execute('UPDATE task_leases SET expires_at=expires_at-100 WHERE task_id=?', (task['id'],))
    assert c.get('/api/v1/tasks/' + task['id'], headers=h).json()['lease']['expired'] is False


def test_claim_count_and_lease_seconds_recorded(env):
    c, h, _, _ = env
    task = newer(env)
    row = c.get('/api/v1/tasks/' + task['id'], headers=h).json()
    assert row['claim_count'] == 1 and row['lease_seconds'] == 300
    assert row['claimed_protocol'] == '1.1'


def test_expired_lease_is_never_reassigned(env):
    c, h, tokens, path = env
    task = newer(env)
    submit(env, key='request-002')
    with sqlite3.connect(path) as db:
        db.execute('UPDATE task_leases SET expires_at=0 WHERE task_id=?', (task['id'],))
    attention = c.get('/api/v1/operator/attention', headers=h).json()
    stale = [row for row in attention['stale_leases'] if row['task_id'] == task['id']]
    assert stale and stale[0]['state'] == 'claimed'
    assert 'not reassigned' in stale[0]['note']
    # The queue must still be blocked; a stale lease is observation, not a retry.
    assert claim(env).json()['task'] is None
    assert c.get('/api/v1/tasks/' + task['id'], headers=h).json()['state'] == 'claimed'


def test_result_closes_lease(env):
    c, h, _, _ = env
    task = newer(env)
    finish(env, task)
    assert c.get('/api/v1/tasks/' + task['id'], headers=h).json()['lease']['closed'] is True


def test_cancel_closes_lease(env):
    c, h, _, _ = env
    task = newer(env)
    c.post('/api/v1/tasks/' + task['id'] + '/cancel', headers=h)
    finish(env, task, 'cancelled')
    assert c.get('/api/v1/tasks/' + task['id'], headers=h).json()['lease']['closed'] is True


def unknown_task(env):
    task = newer(env)
    finish(env, task, 'unknown')
    return task


def test_unknown_blocks_role_until_resolved(env):
    c, h, _, _ = env
    task = unknown_task(env)
    submit(env, key='request-002')
    assert claim(env).json()['task'] is None
    assert c.get('/api/v1/operator/attention', headers=h).json()['unknown_executions'][0]['id'] == task['id']


def test_resolution_requires_note_and_unknown_state(env):
    c, h, _, _ = env
    task = unknown_task(env)
    url = '/api/v1/tasks/' + task['id'] + '/resolve'
    assert c.post(url, headers=h, json={'action': 'confirm_succeeded', 'note': 'ok', 'confirm_task_id': task['id']}).status_code == 422
    assert c.post(url, headers=h, json={'action': 'nonsense', 'note': 'checked manually', 'confirm_task_id': task['id']}).status_code == 422
    assert c.post(url, headers=h, json={'action': 'confirm_succeeded', 'note': 'checked manually', 'confirm_task_id': 'other-task'}).status_code == 422
    resolved = c.post(url, headers=h, json={'action': 'confirm_succeeded', 'note': 'checked manually', 'confirm_task_id': task['id']})
    assert resolved.status_code == 200 and resolved.json()['state'] == 'confirm_succeeded'
    assert c.get('/api/v1/tasks/' + task['id'], headers=h).json()['state'] == 'succeeded'
    # A second resolution is refused; state is no longer unknown.
    assert c.post(url, headers=h, json={'action': 'abandon', 'note': 'second attempt', 'confirm_task_id': task['id']}).status_code == 409
    # The role is unblocked: a new task can now be claimed.
    submit(env, key='request-002')
    assert claim(env).json()['task'] is not None


def test_abandon_unblocks_without_claiming_success(env):
    c, h, _, _ = env
    task = unknown_task(env)
    submit(env, key='request-002')
    resolved = c.post('/api/v1/tasks/' + task['id'] + '/resolve', headers=h,
                      json={'action': 'abandon', 'note': 'host rebooted, outcome not verifiable', 'confirm_task_id': task['id']})
    assert resolved.json()['state'] == 'abandon'
    stored = c.get('/api/v1/tasks/' + task['id'], headers=h).json()
    assert stored['state'] == 'abandoned' and stored['result']['status'] == 'unknown'
    turn = c.get('/api/v1/turns/' + stored['turn_id'], headers=h).json()
    assert turn['state'] == 'completed' and turn['error_code'] is None
    # The task itself still says unknown: abandoning records the ambiguity.
    assert c.get('/api/v1/tasks/' + task['id'], headers=h).json()['state'] == 'abandoned'
    assert claim(env).json()['task'] is not None


def test_resolution_is_audited_and_attributed(env):
    c, h, tokens, _ = env
    task = unknown_task(env)
    c.post('/api/v1/tasks/' + task['id'] + '/resolve', headers=h,
           json={'action': 'confirm_failed', 'note': 'log shows the unit restart loop', 'confirm_task_id': task['id']})
    controls = c.get('/api/v1/tasks/' + task['id'] + '/controls', headers=h).json()
    assert controls[-1]['event'] == 'resolution.confirm_failed' and controls[-1]['actor'] == 'admin'
    assert controls[-1]['details']['note'] == 'log shows the unit restart loop'
    audit = c.get('/api/v1/audit', headers=h).text
    assert 'task.confirm_failed' in audit and 'log shows the unit restart loop' in audit
    # An asset credential must never read the operator trail.
    assert c.get('/api/v1/tasks/' + task['id'] + '/controls', headers=tokens['a']).status_code == 403
    assert c.get('/api/v1/operator/attention', headers=tokens['a']).status_code == 403


def test_attention_reports_offline_assets(env):
    c, h, tokens, path = env
    c.post('/api/v1/agents/a/heartbeat', headers=tokens['a'], json={'instance_id': 'i', 'agent_version': 'v', 'protocol_version': '1.1'})
    with sqlite3.connect(path) as db:
        db.execute('UPDATE agent_presence SET last_seen=0')
    attention = c.get('/api/v1/operator/attention', headers=h).json()
    assert attention['offline_assets'][0]['asset_id'] == 'a'
    assert attention['unknown_executions'] == []


def test_resolution_survives_service_restart(env):
    c, h, _, path = env
    task = unknown_task(env)
    restarted = TestClient(create_app(str(path), ADMIN))
    response = restarted.post('/api/v1/tasks/' + task['id'] + '/resolve', headers=h,
                              json={'action': 'confirm_succeeded', 'note': 'verified in the unit log', 'confirm_task_id': task['id']})
    assert response.status_code == 200
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT state FROM tasks WHERE id=?', (task['id'],)).fetchone()[0] == 'succeeded'
        assert db.execute('SELECT count(*) FROM task_controls WHERE task_id=?', (task['id'],)).fetchone()[0] == 1
