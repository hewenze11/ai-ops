import base64
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from ai_ops.app import create_app
from test_control import env, submit, claim, finish, ADMIN


def newer(env):
    c, _, tokens, _ = env
    submit(env)
    return c.post('/api/v1/agents/a/claim', headers=tokens['a'], json={'protocol_version': '1.1'}).json()['task']


def api(env, task, suffix, body):
    c, _, tokens, _ = env
    return c.post('/api/v1/agents/a/tasks/' + task['id'] + suffix, headers=tokens['a'], json={'claim_id': task['claim_id'], **body})


def chunk(env, task, data=b'abc', offset=0, stream='stdout'):
    return api(env, task, '/output/' + stream + '/chunks', {'offset': offset, 'data': base64.b64encode(data).decode()})


def finalize(env, task, data=b'abc', stream='stdout', complete=True):
    return api(env, task, '/output/' + stream + '/finalize', {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'complete': complete})


def test_heartbeat_auth_and_offline_does_not_release_queue(env):
    c, h, tokens, path = env
    task = newer(env)
    body = {'instance_id': 'test-instance', 'agent_version': 'test', 'protocol_version': '1.1'}
    assert c.post('/api/v1/agents/b/heartbeat', headers=tokens['a'], json=body).status_code == 403
    assert c.post('/api/v1/agents/a/heartbeat', headers=tokens['a'], json=body).status_code == 200
    assert c.get('/api/v1/agents/a/status', headers=h).json()['online']
    with sqlite3.connect(path) as db:
        db.execute('UPDATE agent_presence SET last_seen=0')
    state = c.get('/api/v1/agents/a/status', headers=h).json()
    assert not state['online'] and state['unfinished_tasks'][0]['id'] == task['id']
    submit(env, key='request-002')
    assert claim(env).json()['task'] is None


def test_running_cancel_only_request_then_acknowledge(env):
    c, h, _, _ = env
    task = newer(env)
    submit(env, key='request-002')
    url = '/api/v1/tasks/' + task['id']
    for _ in range(2):
        assert c.post(url + '/cancel', headers=h).json()['state'] == 'cancel_requested'
    assert c.get(url, headers=h).json()['state'] == 'claimed'
    assert claim(env).json()['task'] is None
    assert api(env, task, '/control', {}).json()['cancel_requested']
    assert api(env, task, '/result', {'status': 'cancelled', 'error_code': 'CANCELLED_BY_OPERATOR'}).status_code == 200
    assert c.get(url, headers=h).json()['state'] == 'cancelled'
    assert claim(env).json()['task'] is not None


def test_cancelled_without_request_rejected(env):
    task = newer(env)
    assert api(env, task, '/result', {'status': 'cancelled'}).status_code == 409


def test_cancel_completion_race_keeps_true_result_stops_parent(env):
    c, h, _, _ = env
    task = newer(env)
    url = '/api/v1/tasks/' + task['id']
    c.post(url + '/cancel', headers=h)
    assert finish(env, task).status_code == 200
    stored = c.get(url, headers=h).json()
    assert stored['state'] == 'succeeded'
    assert c.get('/api/v1/turns/' + stored['turn_id'], headers=h).json()['state'] == 'cancelled'


def test_turn_cancel_uses_remote_handshake(env):
    c, h, _, _ = env
    task = newer(env)
    turn = c.get('/api/v1/tasks/' + task['id'], headers=h).json()['turn_id']
    assert c.post('/api/v1/turns/' + turn + '/cancel', headers=h).json()['state'] == 'cancel_requested'
    assert c.get('/api/v1/turns/' + turn, headers=h).json()['state'] == 'waiting_tool'


def test_cancel_unknown_never_unblocks(env):
    c, h, _, _ = env
    task = newer(env)
    c.post('/api/v1/tasks/' + task['id'] + '/cancel', headers=h)
    finish(env, task, 'unknown')
    assert c.post('/api/v1/tasks/' + task['id'] + '/cancel', headers=h).status_code == 409
    submit(env, key='request-002')
    assert claim(env).json()['task'] is None


def test_control_claim_token_and_asset_isolation(env):
    c, _, tokens, _ = env
    task = newer(env)
    bad = {**task, 'claim_id': 'wrong-claim-token-12345'}
    assert api(env, bad, '/control', {}).status_code == 403
    assert c.post('/api/v1/agents/b/tasks/' + task['id'] + '/control', headers=tokens['b'], json={'claim_id': task['claim_id']}).status_code == 404


def test_raw_output_bytes_idempotency_digest_and_download(env):
    c, h, tokens, _ = env
    task = newer(env)
    data = bytes(range(256)) * 600
    for offset in range(0, len(data), 65536):
        part = data[offset:offset + 65536]
        assert chunk(env, task, part, offset).status_code == 200
        assert chunk(env, task, part, offset).json()['duplicate']
    assert chunk(env, task, b'changed').status_code == 409
    assert finalize(env, task, data).status_code == 200
    assert finalize(env, task, data).json()['duplicate']
    assert finalize(env, task, data, complete=False).status_code == 409
    assert chunk(env, task, b'more', len(data)).status_code == 409
    assert finalize(env, task, b'', stream='stderr').status_code == 200
    manifest = c.get('/api/v1/tasks/' + task['id'] + '/output', headers=h).json()
    refs = {r['stream']: {k: (bool(r[k]) if k == 'complete' else r[k]) for k in ('size', 'sha256', 'complete')} for r in manifest}
    result = {'status': 'succeeded', 'exit_code': 0, 'output_archives': refs}
    assert api(env, task, '/result', result).status_code == 200
    assert api(env, task, '/result', result).json()['duplicate']
    # Upload retry after a lost final result ACK remains accepted without mutation.
    assert chunk(env, task, data[:65536]).status_code == 200
    prefix = '/api/v1/tasks/' + task['id'] + '/output/stdout'
    assert c.get(prefix, headers=tokens['a']).status_code == 403
    output = b''
    for offset in range(0, len(data), 45001):
        response = c.get(prefix, headers=h, params={'offset': offset, 'limit': 45001})
        assert response.status_code == 200
        output += response.content
    assert output == data


@pytest.mark.parametrize('body,code', [({'offset': 0, 'data': '!!!!'}, 422), ({'offset': 0, 'data': 'AA=='}, 200), ({'offset': 9, 'data': 'YQ=='}, 409), ({'offset': 64 * 1024 * 1024, 'data': 'YQ=='}, 413)])
def test_chunk_validation(env, body, code):
    task = newer(env)
    assert api(env, task, '/output/stdout/chunks', body).status_code == code


def test_finalize_requires_size_digest_and_claim(env):
    task = newer(env)
    chunk(env, task)
    assert finalize(env, task, b'wrong').status_code == 409
    assert finalize(env, task, b'xyz').status_code == 409
    assert finalize(env, {**task, 'claim_id': 'not-the-claim-123456789'}).status_code == 403
    assert api(env, task, '/result', {'status': 'failed', 'output_archives': {'stdout': {}}}).status_code == 422
    assert api(env, task, '/result', {'status': 'failed', 'output_archives': {'stdout': {}, 'stderr': {}}}).status_code == 409


def test_archive_and_cancel_survive_service_restart(env):
    c, h, tokens, path = env
    task = newer(env)
    chunk(env, task)
    c.post('/api/v1/tasks/' + task['id'] + '/cancel', headers=h)
    restarted = TestClient(create_app(str(path), ADMIN))
    reenv = restarted, h, tokens, path
    assert api(reenv, task, '/control', {}).json()['cancel_requested']
    assert chunk(reenv, task).json()['duplicate']
    assert finalize(reenv, task).status_code == 200


def test_audit_failure_rolls_back_chunk(env):
    _, _, _, path = env
    task = newer(env)
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE audit')
    with pytest.raises(sqlite3.OperationalError):
        chunk(env, task)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM output_chunks').fetchone()[0] == 0


def test_legacy_result_without_archive_field_remains_idempotent(env):
    _, _, _, path = env
    task = newer(env)
    assert finish(env, task).status_code == 200
    with sqlite3.connect(path) as db:
        row = db.execute('SELECT result FROM tasks WHERE id=?', (task['id'],)).fetchone()
        old = json.loads(row[0]); old.pop('output_archives', None)
        db.execute('UPDATE tasks SET result=? WHERE id=?', (json.dumps(old), task['id']))
    assert finish(env, task).json()['duplicate']
