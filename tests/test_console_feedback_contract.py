"""Regression coverage for the October console acceptance feedback."""
import sqlite3
from test_control import env
from test_connector_managed_secrets import provision, SECRET


def test_new_console_roles_never_inherit_global_provider(env):
    c, h, _, _ = env
    url = '/api/v1/console/roles'
    for settings in ({'enabled': True}, {'enabled': True, 'provider_mode': 'inherit'},
                     {'enabled': True, 'model': 'm', 'base_url': 'https://example.com/v1'}):
        response = c.post(url, headers=h, json={'id': 'new', 'name': 'new', 'model': settings})
        assert response.status_code == 422, response.text
    response = c.post(url, headers=h, json={'id': 'new', 'name': 'new'})
    assert response.status_code == 201
    config = c.get('/api/v1/roles/new/model', headers=h).json()
    assert config['provider_mode'] == 'independent'
    assert config['base_url'] == '' and not config['enabled'] and not config['api_key_configured']


def test_new_role_key_is_not_shared_with_another_role(env):
    c, h, _, _ = env
    config = {'enabled': True, 'model': 'm', 'base_url': 'https://example.com/v1', 'api_key': 'role-one-only'}
    assert c.post('/api/v1/console/roles', headers=h, json={'id': 'a', 'name': 'a', 'model': config}).status_code == 201
    config.pop('api_key')
    assert c.post('/api/v1/console/roles', headers=h, json={'id': 'b', 'name': 'b', 'model': config}).status_code == 422
    assert c.put('/api/v1/roles/a/model', headers=h, json={**config, 'provider_mode': 'independent'}).status_code == 200
    assert c.get('/api/v1/roles/a/model', headers=h).json()['api_key_configured']


def test_console_role_prompt_commits_with_provider(env):
    c, h, _, path = env
    config = {'enabled': True, 'model': 'm', 'base_url': 'https://example.com/v1', 'api_key': 'own-key'}
    body = {'id': 'atomic-role', 'name': 'Atomic', 'prompt': 'Use evidence first.', 'model': config}
    assert c.post('/api/v1/console/roles', headers=h, json=body).status_code == 201
    roles = c.get('/api/v1/roles', headers=h).json()
    assert next(r for r in roles if r['id'] == 'atomic-role')['prompt'] == 'Use evidence first.'
    body['id'] = 'invalid-role'
    body['model']['api_key'] = ''
    assert c.post('/api/v1/console/roles', headers=h, json=body).status_code == 422
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM roles WHERE id='invalid-role'").fetchone() is None
        assert db.execute("SELECT 1 FROM role_prompts WHERE role_id='invalid-role'").fetchone() is None


def test_ssh_login_credential_is_indexed_and_repointed_without_secret(env):
    c, h, _, path = env
    assert provision(env).status_code == 201
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT id,source,kind FROM credential_handles WHERE asset_id='managed'").fetchone()
    assert row == ('connector-login', 'ssh://reader@192.0.2.10:22', 'connector')
    assert c.put('/api/v1/assets/managed/connection', headers=h, json={'ssh_host': '192.0.2.11'}).status_code == 200
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT source FROM credential_handles WHERE asset_id='managed'").fetchall()
        audit = str(db.execute('SELECT * FROM credential_events').fetchall())
    assert rows == [('ssh://reader@192.0.2.11:22',)]
    assert SECRET not in audit
    assert SECRET.encode() not in path.read_bytes()


def test_builtin_mode_names_rejected_by_backend(env):
    c, h, _, _ = env
    modes = c.get('/api/v1/modes', headers=h).json()
    for mode in modes:
        if mode['builtin']:
            r = c.put('/api/v1/modes/' + mode['id'], headers=h, json={'name': 'renamed'})
            assert r.status_code == 409, r.text
            assert c.get('/api/v1/modes/' + mode['id'], headers=h).json()['name'] == mode['name']
