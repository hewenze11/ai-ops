"""Discovery: a read that hits a secret creates a handle (§42).

The flow under test is the product's whole answer to "the AI needs the password":
it reads the file, gets a masked body plus a handle, and can only ever refer to
``cred:<id>`` afterwards. If the value reached the model anywhere in this flow,
the design would be decorative.
"""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ai_ops import turns
from ai_ops.app import Result, create_app

ADMIN = 'discovery-stage4-admin-token-1234567890'


@pytest.fixture
def env(tmp_path):
    path = tmp_path / 'discovery.db'
    app = create_app(str(path), ADMIN)
    client = TestClient(app)
    headers = {'Authorization': 'Bearer ' + ADMIN}
    client.post('/api/v1/roles', json={'id': 'ops', 'name': 'Ops'}, headers=headers)
    client.post('/api/v1/assets', json={'id': 'host1', 'name': 'H', 'allowed_users': ['reader']}, headers=headers)
    return client, headers, path


def test_the_model_sees_handles_and_a_notice_not_values(env):
    c, h, path = env
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute('INSERT INTO tasks(id,role_id,asset_id,payload,state,idempotency_key,created_at,updated_at) '
                   "VALUES('t1','ops','host1','{}','succeeded','k',0,0)")
        db.execute('UPDATE tasks SET result=? WHERE id=?', (json.dumps({
            'claim_id': 'c' * 16, 'status': 'succeeded', 'exit_code': 0,
            'stdout': 'DB_PASSWORD=[REDACTED]',
            'credentials': [{'id': 'cred-abc', 'name': 'cred-abc', 'source': '/srv/app/.env'}]}), 't1'))
        row = db.execute("SELECT * FROM tasks WHERE id='t1'").fetchone()
    body = json.loads(turns._tool_output(row))
    assert body['stdout'] == 'DB_PASSWORD=[REDACTED]'
    assert body['credentials_found'] == [{'id': 'cred-abc', 'name': 'cred-abc'}]
    assert 'cred:<id>' in body['credential_notice']
    # The raw handle list is replaced, not merely supplemented: there is one
    # representation of a handle and it has no value field.
    assert 'credentials' not in body


def test_a_result_without_handles_is_passed_through(env):
    c, h, path = env
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute('INSERT INTO tasks(id,role_id,asset_id,payload,state,idempotency_key,created_at,updated_at) '
                   "VALUES('t2','ops','host1','{}','succeeded','k2',0,0)")
        db.execute('UPDATE tasks SET result=? WHERE id=?', (json.dumps({'status': 'succeeded', 'stdout': 'ok'}), 't2'))
        row = db.execute("SELECT * FROM tasks WHERE id='t2'").fetchone()
    assert json.loads(turns._tool_output(row)) == {'status': 'succeeded', 'stdout': 'ok'}


def test_a_value_field_is_refused_rather_than_ignored():
    # A schema that accepts and drops a value field invites a client to believe
    # it was stored. This one says no.
    with pytest.raises(ValueError):
        Result.model_validate({'claim_id': 'c' * 16, 'status': 'succeeded', 'exit_code': 0,
                               'credentials': [{'id': 'cred-hit', 'name': 'x', 'value': 'plaintext'}]})


def test_reporting_a_handle_files_it_under_the_reporting_asset(env):
    c, h, path = env
    created = c.post('/api/v1/assets', headers=h,
                     json={'id': 'host9', 'name': 'H9', 'allowed_users': ['reader']})
    agent_auth = {'Authorization': 'Bearer ' + created.json()['agent_token']}
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO tasks(id,role_id,asset_id,payload,state,claim_id,idempotency_key,created_at,updated_at) "
                   "VALUES('t4','ops','host9','{}','claimed',lower(hex(randomblob(16))),'k4',0,0)")
        claim = db.execute("SELECT claim_id FROM tasks WHERE id='t4'").fetchone()[0]
    body = {'claim_id': claim, 'status': 'succeeded', 'exit_code': 0, 'stdout': 'ok',
            'credentials': [{'id': 'cred-hit', 'name': 'app token', 'source': '/etc/app/token'}]}
    response = c.post('/api/v1/agents/host9/tasks/t4/result', headers=agent_auth, json=body)
    assert response.status_code == 200, response.text
    items = c.get('/api/v1/assets/host9/credentials', headers=h).json()['items']
    assert [item['id'] for item in items] == ['cred-hit']
