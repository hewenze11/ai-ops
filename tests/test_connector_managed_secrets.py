"""Managed SSH API-to-executor credential path; no real SSH connections."""
import sqlite3
from pathlib import Path

import paramiko
import pytest
from ai_ops import connector_ssh
from ai_ops.connector_secrets import MAGIC, key_path
from test_control import env

SECRET = 'test-only-ssh-password-DO-NOT-USE'


def provision(env, **changes):
    client, headers, _, _ = env
    body = dict(id='managed', name='Managed SSH', allowed_users=['reader'],
                connection_type='ssh', ssh_host='192.0.2.10', ssh_user='reader',
                ssh_auth_kind='password', ssh_secret=SECRET,
                ssh_host_key='ssh-ed25519 test-public-key')
    body.update(changes)
    return client.post('/api/v1/assets', headers=headers, json=body)


def ref(env):
    with sqlite3.connect(env[3]) as db:
        return Path(db.execute("SELECT ssh_secret_ref FROM asset_connections WHERE asset_id='managed'").fetchone()[0])


def test_create_encrypts_and_executor_decrypts_without_disclosure(env):
    client, headers, _, db_path = env
    result = provision(env)
    assert result.status_code == 201, result.text
    secret_file = ref(env)
    assert secret_file.read_bytes().startswith(MAGIC)
    assert SECRET.encode() not in secret_file.read_bytes()
    assert SECRET.encode() not in db_path.read_bytes()
    assert connector_ssh._load_secret(str(secret_file)) == SECRET.encode()
    for response in (result, client.get('/api/v1/assets', headers=headers),
                     client.get('/api/v1/assets/managed/connection', headers=headers)):
        assert SECRET not in response.text
        assert 'ssh_secret_ref' not in response.text


def test_update_preserves_or_replaces_credential(env):
    client, headers, _, _ = env
    assert provision(env).status_code == 201
    old = ref(env)
    url = '/api/v1/assets/managed/connection'
    assert client.put(url, headers=headers, json={'ssh_host': '192.0.2.11'}).status_code == 200
    assert ref(env) == old
    result = client.put(url, headers=headers, json={'ssh_secret': SECRET + '-rotated'})
    assert result.status_code == 200, result.text
    assert ref(env) != old
    assert connector_ssh._load_secret(str(ref(env))) == (SECRET + '-rotated').encode()
    assert SECRET not in result.text
    assert SECRET.encode() not in env[3].read_bytes()


def test_conflicting_inputs_and_failed_writes_do_not_leak_or_leave_ciphertext(env):
    client, headers, _, db_path = env
    assert provision(env, ssh_secret_ref='/legacy/path').status_code == 422
    assert provision(env).status_code == 201
    before = set((db_path.parent / 'connector-secrets').glob('*.enc'))
    assert provision(env).status_code == 409
    result = client.put('/api/v1/assets/absent/connection', headers=headers, json={'ssh_secret': SECRET})
    assert result.status_code == 404
    conflict = client.put('/api/v1/assets/managed/connection', headers=headers,
                          json={'ssh_secret': SECRET, 'ssh_secret_ref': '/legacy/path'})
    assert conflict.status_code == 422
    assert SECRET not in conflict.text
    assert set((db_path.parent / 'connector-secrets').glob('*.enc')) == before


@pytest.mark.parametrize('damage', ['missing-key', 'bad-key', 'bad-ciphertext', 'missing-envelope'])
def test_damaged_managed_credentials_fail_closed(env, damage):
    assert provision(env).status_code == 201
    path = ref(env)
    if damage == 'missing-key':
        key_path(path.parent).unlink()
    elif damage == 'bad-key':
        key_path(path.parent).write_bytes(b'not-a-key')
    elif damage == 'bad-ciphertext':
        path.write_bytes(MAGIC + b'corrupt')
    else:
        path.write_bytes(b'never-treat-as-plaintext')
    with pytest.raises(paramiko.SSHException) as error:
        connector_ssh._load_secret(str(path))
    assert connector_ssh._classify_connect_error(error.value) == 'SECRET_DECRYPT_FAILED'
    assert SECRET not in str(error.value)


def test_legacy_plaintext_reference_still_supported(env, tmp_path):
    path = tmp_path / 'legacy.password'
    path.write_text(SECRET, encoding='utf-8')
    assert provision(env, ssh_secret=None, ssh_secret_ref=str(path)).status_code == 201
    assert connector_ssh._load_secret(str(ref(env))) == SECRET.encode()
