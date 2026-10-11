"""The console's real registration alias must support the same write-only SSH flow."""
from pathlib import Path

import pytest
from ai_ops.connector_secrets import refs_in, root_for
from ai_ops.connector_ssh import _load_secret
from test_control import env


def body(kind='password'):
    return dict(id='console-ssh', name='Console SSH', connection_type='ssh',
                allowed_users=['reader'], account_notes={'reader': 'inspect'},
                ssh_host='192.0.2.10', ssh_user='reader', ssh_auth_kind=kind,
                ssh_host_key='ssh-ed25519 fixture-public-key',
                ssh_secret='  fixture-password  ' if kind == 'password' else
                '-----BEGIN OPENSSH PRIVATE KEY-----\nfixture-key\n-----END OPENSSH PRIVATE KEY-----\n')


@pytest.mark.parametrize('kind', ['password', 'key'])
def test_console_create_encrypts_exact_secret_without_leaking(env, kind):
    c, h, _, path = env
    payload = body(kind)
    result = c.post('/api/v1/console/assets', headers=h, json=payload)
    assert result.status_code == 201, result.text
    assert result.json()['asset_id'] == payload['id']
    reference = refs_in(path)[0]
    assert _load_secret(reference).decode() == payload['ssh_secret']
    assert payload['ssh_secret'].encode() not in Path(reference).read_bytes()
    assert payload['ssh_secret'].encode() not in path.read_bytes()
    assert 'ssh_secret' not in result.text and 'fixture-password' not in result.text and 'fixture-key' not in result.text
    detail = c.get('/api/v1/assets/console-ssh/connection', headers=h)
    assert 'ssh_secret' not in detail.text
    changed = c.put('/api/v1/assets/console-ssh/connection', headers=h, json={'ssh_host': '192.0.2.11'})
    assert changed.status_code == 200
    assert refs_in(path) == [reference]
    changed = c.put('/api/v1/assets/console-ssh/connection', headers=h, json={'ssh_secret': 'replacement-fixture'})
    assert changed.status_code == 200
    assert _load_secret(refs_in(path)[0]) == b'replacement-fixture'


@pytest.mark.parametrize('change', [dict(ssh_secret_ref='/legacy/path'),
    dict(ssh_host_key=None), dict(connection_type='agent'), dict(ssh_secret='')],
    ids=['conflicting-sources', 'missing-pinned-key', 'agent-secret', 'empty-secret'])
def test_console_invalid_credential_request_does_not_leak_or_write(env, change):
    c, h, _, path = env
    payload = body()
    payload.update(change)
    result = c.post('/api/v1/console/assets', headers=h, json=payload)
    assert result.status_code == 422
    assert 'fixture-password' not in result.text
    assert refs_in(path) == []
    assert not list(root_for(path).glob('*.enc'))


def test_console_duplicate_rolls_back_new_ciphertext(env):
    c, h, _, path = env
    assert c.post('/api/v1/console/assets', headers=h, json=body()).status_code == 201
    before = set(root_for(path).iterdir())
    assert c.post('/api/v1/console/assets', headers=h, json=body()).status_code == 409
    assert set(root_for(path).iterdir()) == before
