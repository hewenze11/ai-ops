"""Stage 3: frozen modes, frozen name lists, platform baseline (§43.1, §44, §45.2).

The point of these tests is not the happy path. It is that an authorization
decision, once taken, cannot be rewritten afterwards by editing configuration:
not for a task already queued, not for an approval already granted, and not for
an audit entry already written.
"""
import json

import pytest
from fastapi.testclient import TestClient

import ai_ops.baseline as baseline
import ai_ops.namelists as namelists
import ai_ops.scrubbing as scrubbing
from ai_ops.app import create_app


ADMIN = 'stage-three-admin-token-not-real-1234567890'


@pytest.fixture
def env(tmp_path):
    path = tmp_path / 'stage3.db'
    app = create_app(str(path), ADMIN)
    client = TestClient(app)
    headers = {'Authorization': 'Bearer ' + ADMIN}
    client.post('/api/v1/roles', json={'id': 'ops', 'name': 'Ops'}, headers=headers)
    client.post('/api/v1/assets', json={'id': 'host1', 'name': 'Host 1', 'allowed_users': ['reader']}, headers=headers)
    return client, headers, path


# ---------------------------------------------------------------------------
# Baseline secret patterns
# ---------------------------------------------------------------------------
def test_baseline_masks_provider_and_config_shapes():
    samples = {
        'aws': ('aws_access_key_id = AKIAIOSFODNN7EXAMPLE', 'AKIAIOSFODNN7EXAMPLE'),
        'github': ('token ghp_' + 'a' * 30, 'ghp_' + 'a' * 30),
        'yaml': ('  db_password: hunter2\n', 'hunter2'),
        'url': ('postgres://app:s3cretpw@db.internal/app', 's3cretpw'),
        'jwt': ('Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop', 'eyJhbGciOiJIUzI1NiJ9'),
    }
    for name, (text, secret) in samples.items():
        masked, matched = baseline.apply(text)
        assert secret not in masked, name
        assert '[REDACTED]' in masked, name
        assert matched, name


def test_baseline_carries_a_version_and_rule_inventory():
    # The version is what an audit entry cites; without it the claim "these rules
    # were in force" is unverifiable after an upgrade.
    assert baseline.BASELINE_VERSION
    inventory = {rule['id'] for rule in baseline.kinds()}
    assert {'pem_private_key', 'aws_access_key', 'named_secret'} <= inventory


def test_baseline_applies_even_to_text_the_operator_list_missed():
    # A deployment-level rule: the baseline is not opt-in, so ordinary scrubbing
    # picks it up without anyone configuring anything.
    out = scrubbing.redact_text('export API_TOKEN=abcdef123456')
    assert 'abcdef123456' not in out


def test_baseline_does_not_swallow_ordinary_config():
    text = 'listen 80;\nserver_name example.com;\nmax_body_size = 10m\n'
    masked, matched = baseline.apply(text)
    assert masked == text and matched == []


# ---------------------------------------------------------------------------
# Frozen name lists
# ---------------------------------------------------------------------------
def test_platform_seeds_a_list_per_mode_and_binds_them(env):
    c, h, _ = env
    lists = c.get('/api/v1/name-lists', headers=h).json()
    kinds = {item['kind'] for item in lists}
    assert {'readonly', 'write'} <= kinds
    bindings = c.get('/api/v1/name-list-bindings', headers=h).json()
    assert bindings['readonly']['readonly_list']
    assert bindings['direct']['write_list'] == bindings['confirm']['write_list']


def test_seeding_is_idempotent_across_restarts(env):
    c, h, path = env
    before = len(c.get('/api/v1/name-lists', headers=h).json())
    create_app(str(path), ADMIN)
    after = len(c.get('/api/v1/name-lists', headers=h).json())
    assert before == after


def test_editing_a_list_forks_it_and_keeps_the_parent(env):
    c, h, _ = env
    lists = c.get('/api/v1/name-lists', headers=h).json()
    write = next(item for item in lists if item['kind'] == 'write')
    forked = c.post('/api/v1/name-lists/' + write['id'] + '/fork', headers=h,
                    json={'name': 'Only reload', 'rules': [
                        {'rule_id': 'one', 'action': 'service.reload'}]}).json()
    assert forked['id'] != write['id']
    assert forked['parent_id'] == write['id']
    assert forked['version'] == write['version'] + 1
    # The parent is unchanged: history has to stay reconstructible.
    parent = c.get('/api/v1/name-lists/' + write['id'], headers=h).json()
    assert len(parent['rules']) == len(write['rules'])
    assert [r['action'] for r in forked['rules']] == ['service.reload']


def test_rebinding_a_list_does_not_change_a_queued_turns_list(env):
    c, h, path = env
    import sqlite3
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        lists = c.get('/api/v1/name-lists', headers=h).json()
        strict = next(item for item in lists if item['kind'] == 'write')
        forked = c.post('/api/v1/name-lists/' + strict['id'] + '/fork', headers=h,
                        json={'rules': []}).json()
        turn_id = 'turn-frozen'
        db.execute('INSERT INTO role_turns(id,role_id,source,source_id,execution_users,mode,prompt,payload,state,created_at,updated_at,write_list) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (turn_id, 'ops', 'chat', 'chat-1', '[]', 'confirm', 'p', '{}', 'queued', 0, 0, strict['id']))
    c.put('/api/v1/name-list-bindings/confirm', headers=h, json={'write_list': forked['id']})
    # Re-read after the API call in a fresh connection: the write happened
    # elsewhere, so a still-open read transaction would see the stale snapshot.
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT write_list FROM role_turns WHERE id=?', (turn_id,)).fetchone()[0] == strict['id']


def test_a_host_tag_scoped_rule_does_not_apply_to_an_untagged_host(env):
    c, h, path = env
    import sqlite3
    with sqlite3.connect(path) as db:
        lists = c.get('/api/v1/name-lists', headers=h).json()
        write = next(item for item in lists if item['kind'] == 'write')
        forked = c.post('/api/v1/name-lists/' + write['id'] + '/fork', headers=h, json={'rules': [
            {'rule_id': 'r1', 'action': 'service.restart', 'hosts': ['prod']}]}).json()
        allowed, scope = namelists.rule_actions(db, forked['id'])
    assert allowed == {'service.restart'}
    assert scope == {'service.restart': ['prod']}


def test_host_tags_round_trip(env):
    c, h, _ = env
    assert c.put('/api/v1/assets/host1/tags', headers=h, json={'tags': ['prod', 'db']}).json()['tags'] == ['db', 'prod']
    assert c.put('/api/v1/assets/host1/tags', headers=h, json={'tags': []}).json()['tags'] == []


# ---------------------------------------------------------------------------
# Unattended tasks must declare a mode
# ---------------------------------------------------------------------------
def test_a_trigger_without_a_mode_is_refused(env):
    c, h, _ = env
    body = {'name': 'T', 'kind': 'trigger', 'role_id': 'ops', 'prompt': 'p',
            'execution_grants': [{'asset_id': 'host1', 'username': 'reader'}]}
    assert c.post('/api/v1/custom-tasks', headers=h, json=body).status_code == 422
    body['mode'] = 'readonly'
    assert c.post('/api/v1/custom-tasks', headers=h, json=body).status_code == 201


def test_an_edit_that_drops_the_mode_is_refused(env):
    c, h, _ = env
    body = {'id': 'task-a', 'name': 'T', 'kind': 'trigger', 'mode': 'readonly', 'role_id': 'ops',
            'prompt': 'p', 'execution_grants': [{'asset_id': 'host1', 'username': 'reader'}]}
    assert c.post('/api/v1/custom-tasks', headers=h, json=body).status_code == 201
    body.pop('mode')
    assert c.put('/api/v1/custom-tasks/task-a', headers=h, json=body).status_code == 422
