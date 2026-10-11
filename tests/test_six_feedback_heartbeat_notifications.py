"""Compact heartbeat history and safe per-task webhook defaults (no network)."""
import base64
import json
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ai_ops.app import create_app
from ai_ops.alert_notify import collect_conclusions
from ai_ops.unified_logs import query_heartbeat_records

TOKEN = 'a' * 40
ADMIN = {'Authorization': 'Bearer ' + TOKEN}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr('ai_ops.alert_notify.validate_url', lambda url: None)
    return TestClient(create_app(str(tmp_path / 'test.db'), TOKEN, 'test'), headers=ADMIN)


def task_body(identity, **extra):
    return {'id': identity, 'name': identity, 'kind': 'trigger', 'role_id': 'ops',
            'prompt': 'Inspect only', 'execution_users': ['reader'], 'mode': 'readonly', **extra}


def role(client):
    assert client.post('/api/v1/roles', json={'id': 'ops', 'name': 'Ops'}).status_code == 201


def finish(client, saved):
    reply = client.post('/api/v1/triggers/' + saved['id'] + '/invoke',
                        headers={'Authorization': 'Bearer ' + saved['trigger_token']},
                        json={'status': 'firing'}).json()
    with client.app.state.transaction() as db:
        db.execute("UPDATE role_turns SET state='completed',final_text='Checked' WHERE id=?", (reply['turn_id'],))
    return reply['turn_id']


def test_new_defaults_disabled_old_null_missing_preserved_and_explicit_null(client):
    role(client)
    saved = client.post('/api/v1/custom-tasks', json=task_body('new')).json()
    assert saved['notification_mode'] == 'disabled'
    assert saved['notification']['enabled'] is False
    # Historical rows retain their previous inheritance meaning on GET and PUT.
    for identity, absent in [('old-null', False), ('old-missing', True)]:
        created = client.post('/api/v1/custom-tasks', json=task_body(identity)).json()
        with client.app.state.transaction() as db:
            cfg = json.loads(db.execute('SELECT config FROM custom_tasks WHERE id=?', (identity,)).fetchone()[0])
            if absent:
                cfg.pop('notification')
            else:
                cfg['notification'] = None
            db.execute('UPDATE custom_tasks SET config=? WHERE id=?', (json.dumps(cfg), identity))
        assert client.get('/api/v1/custom-tasks/' + identity).json()['notification_mode'] == 'legacy_inherit'
        result = client.put('/api/v1/custom-tasks/' + identity, json=task_body(identity)).json()
        assert result['notification'] is None and result['notification_mode'] == 'legacy_inherit'
    # Historical empty objects mean disabled; omission must not turn them into null.
    with client.app.state.transaction() as db:
        cfg = json.loads(db.execute("SELECT config FROM custom_tasks WHERE id='new'").fetchone()[0])
        cfg['notification'] = {}
        db.execute('UPDATE custom_tasks SET config=? WHERE id=?', (json.dumps(cfg), 'new'))
    result = client.put('/api/v1/custom-tasks/new', json=task_body('new')).json()
    assert result['notification_mode'] == 'disabled' and result['notification']['enabled'] is False
    explicit = client.post('/api/v1/custom-tasks', json=task_body('explicit', notification=None)).json()
    assert explicit['notification_mode'] == 'legacy_inherit'
    assert client.put('/api/v1/custom-tasks/new', json=task_body('new')).json()['notification_mode'] == 'disabled'
    assert client.put('/api/v1/custom-tasks/explicit', json=task_body('explicit', notification={'enabled': False})).json()['notification_mode'] == 'disabled'
    assert not client.get('/api/v1/runtime/notifications').json()['worker_running']


def test_task_webhook_isolation_snapshot_global_legacy_and_no_real_sends(client):
    role(client)
    global_url = 'https://example.com/global-secret'
    assert client.put('/api/v1/alert-notify/config', json={'enabled': True, 'url': global_url, 'channel': 'generic'}).status_code == 200
    saved = {}
    for identity in ('a', 'b'):
        body = task_body(identity, notification={'enabled': True, 'url': 'https://example.com/' + identity + '-secret', 'channel': 'generic'})
        saved[identity] = client.post('/api/v1/custom-tasks', json=body).json()
        assert saved[identity]['notification_mode'] == 'task_webhook'
        # Omitted notification and the redacted URL both preserve only this task's secret.
        assert client.put('/api/v1/custom-tasks/' + identity, json=task_body(identity)).status_code == 200
        body['notification'] = saved[identity]['notification']
        assert client.put('/api/v1/custom-tasks/' + identity, json=body).status_code == 200
    saved['off'] = client.post('/api/v1/custom-tasks', json=task_body('off')).json()
    saved['legacy'] = client.post('/api/v1/custom-tasks', json=task_body('legacy', notification=None)).json()
    with patch('ai_ops.alert_notify.post_once', side_effect=AssertionError('No real sends permitted')) as sender:
        turns = {identity: finish(client, config) for identity, config in saved.items()}
        # Route is captured by the accepted event, not rewritten by later edits.
        assert client.put('/api/v1/custom-tasks/a', json=task_body('a', notification={'enabled': False})).status_code == 200
        assert collect_conclusions(client.app.state.transaction, client.app.state.audit) == 3
        assert collect_conclusions(client.app.state.transaction, client.app.state.audit) == 0
        sender.assert_not_called()
    with client.app.state.transaction() as db:
        routes = {r['turn_id']: r['url'] for r in db.execute('SELECT turn_id,url FROM alert_notifications')}
    assert routes == {turns['a']: 'https://example.com/a-secret', turns['b']: 'https://example.com/b-secret', turns['legacy']: global_url}
    assert not client.get('/api/v1/runtime/notifications').json()['worker_running']
    assert 'a-secret' not in client.get('/api/v1/custom-task-events').text


def seed_samples(client):
    for identity in ('a', 'b'):
        assert client.post('/api/v1/assets', json={'id': identity, 'name': identity, 'allowed_users': ['reader']}).status_code == 201
    with client.app.state.transaction() as db:
        rows = [('a', 1000, 1, 'ours', None, '1.2'),
                ('a', 1010, 2, 'ours', 'ours', '1.2'),
                ('a', 1020, 3, 'ours', 'other', '1.2'),
                ('a', 1060, None, None, 'other', '1.2'),
                ('b', 1000, None, None, None, '1.1')]
        db.executemany('INSERT INTO asset_state_samples(asset_id,received_at,state,controller,busy_by,protocol) VALUES(?,?,?,?,?,?)', rows)


def test_heartbeat_compact_table_paging_ttl_unknown_retention_readonly(client):
    seed_samples(client)
    with client.app.state.transaction() as db:
        first = query_heartbeat_records(db, asset_id='a', limit=2, now=1200)
        assert [r['state'] for r in first['items']] == [None, 4]
        assert first['items'][0]['state_label'] == '未知'
        assert first['items'][0]['ttl_seconds'] is None
        expiry = first['items'][1]
        assert expiry['timestamp'] == 1050 and expiry['derived'] is True
        assert expiry['observed_at'] == 1020 and expiry['ttl_source'] == 'server_presence_lease'
        assert expiry['ack_detail_path'] == '/api/v1/assets/a/heartbeats?view=attempts'
        second = query_heartbeat_records(db, asset_id='a', before=first['next_before'], limit=2, now=1200)
        third = query_heartbeat_records(db, asset_id='a', before=second['next_before'], limit=2, now=1200)
        assert [r['state'] for r in second['items'] + third['items']] == [3, 2, 1]
        assert third['next_before'] is None
        all_rows = query_heartbeat_records(db, now=1200)['items']
        assert len(all_rows) == 6
        assert len({r['id'] for r in all_rows}) == 6
        assert [r['state'] for r in all_rows if r['asset_id'] == 'b'] == [None]
        assert query_heartbeat_records(db, now=100000)['items'] == []
        # No synthetic row/document/ACK is persisted by read projections.
        assert db.execute('SELECT COUNT(*) FROM asset_state_samples').fetchone()[0] == 5
        assert db.execute('SELECT COUNT(*) FROM heartbeat_attempts').fetchone()[0] == 0


def test_unknown_heartbeat_interrupts_ttl_and_protocol_cannot_fake_state(client):
    seed_samples(client)
    with client.app.state.transaction() as db:
        db.execute("INSERT INTO asset_state_samples(asset_id,received_at,state,protocol) VALUES('a',1040,NULL,'1.2')")
        db.execute("INSERT INTO asset_state_samples(asset_id,received_at,state,protocol) VALUES('b',1040,1,'1.1')")
        rows = query_heartbeat_records(db, now=1200)['items']
        assert not any(row['state'] == 4 for row in rows)
        assert all(row['state'] is None for row in rows if row['asset_id'] == 'b')


def test_heartbeat_api_admin_default_feed_and_input_validation(client):
    seed_samples(client)
    route = '/api/v1/logs/heartbeats'
    assert TestClient(client.app).get(route).status_code == 401
    assert TestClient(client.app, headers={'Authorization': 'Bearer invalid'}).get(route).status_code in (401, 403)
    with patch('ai_ops.unified_logs.time.time', return_value=1200):
        result = client.get(route, params={'asset_id': 'a', 'limit': 2})
        assert result.status_code == 200 and len(result.json()['items']) == 2
        assert client.get(route + '?limit=201').status_code == 422
        assert client.get(route + '?before=bad').status_code == 422
        assert client.get(route, params={'asset_id': 'b', 'before': result.json()['next_before']}).status_code == 422
        cursor = base64.urlsafe_b64encode(json.dumps([float('nan'), 'id', 1200, None]).encode()).decode()
        assert client.get(route, params={'before': cursor}).status_code == 422
    assert all(r['kind'] != 'heartbeat' for r in client.get('/api/v1/logs').json()['items'])
    assert len(client.get('/api/v1/logs?kind=heartbeat').json()['items']) == 5
    with client.app.state.transaction() as db:
        for limit in (0, 201, True):
            with pytest.raises(HTTPException):
                query_heartbeat_records(db, limit=limit, now=1200)
