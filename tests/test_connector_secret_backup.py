"""Snapshot/restore of managed SSH credentials, including failed installation."""
import os
import sqlite3
from pathlib import Path
from contextlib import closing

import pytest
from ai_ops import backup
from ai_ops.connector_secrets import ConnectorSecrets, key_path, root_for, refs_in, MAGIC
from ai_ops.connector_ssh import _load_secret
from test_control import env
from test_connector_managed_secrets import provision, ref, SECRET


def destination(tmp_path):
    live = tmp_path / 'destination' / 'control.db'
    live.parent.mkdir()
    secret = ConnectorSecrets(live).write('existing-destination-secret')
    with closing(sqlite3.connect(live)) as db:
        db.execute('PRAGMA user_version=1')
        db.execute('CREATE TABLE asset_connections(asset_id TEXT, ssh_secret_ref TEXT)')
        db.execute('INSERT INTO asset_connections VALUES(?,?)', ('existing', secret))
        db.commit()
    return live, Path(secret)


def snapshot(env, tmp_path):
    assert provision(env).status_code == 201
    out = tmp_path / 'backup' / 'snapshot.db'
    meta = backup.create_backup(env[3], out)
    return out, meta


def test_roundtrip_uses_destination_key_without_original_machine(env, tmp_path):
    out, meta = snapshot(env, tmp_path)
    original = ref(env)
    original_key = key_path(original.parent).read_bytes()
    saved_secret = Path(str(out) + '.connector-secrets') / original.name
    saved_key = Path(str(out) + '.connector-keys') / (original.stem + '.key')
    assert meta['connector_secret_count'] == 1 and meta['connector_secrets_included']
    assert saved_secret.read_bytes().startswith(MAGIC)
    assert SECRET.encode() not in saved_secret.read_bytes()
    assert saved_key.read_bytes() == original_key
    # Restore must be independent of original absolute paths and original key.
    original.unlink()
    key_path(original.parent).unlink()
    backup.verify_backup(out)
    live, previous_secret = destination(tmp_path)
    destination_key = key_path(root_for(live)).read_bytes()
    result = backup.restore_backup(out, live)
    restored_ref = Path(refs_in(live)[0])
    assert restored_ref.parent == root_for(live)
    assert restored_ref != original
    assert _load_secret(str(restored_ref)) == SECRET.encode()
    assert key_path(root_for(live)).read_bytes() == destination_key != original_key
    assert SECRET.encode() not in live.read_bytes()
    assert _load_secret(str(previous_secret)) == b'existing-destination-secret'
    assert refs_in(result['previous_kept_at']) == [str(previous_secret.resolve())]
    assert not list(live.parent.glob('*.restore-incoming*'))
    # Re-backed-up restored data must also be independently verifiable.
    second = tmp_path / 'second.db'
    assert backup.create_backup(live, second)['connector_secret_count'] == 1
    backup.verify_backup(second)


@pytest.mark.parametrize('component,damage', [('connector-secrets', 'missing'),
    ('connector-secrets', 'corrupt'), ('connector-keys', 'missing'), ('connector-keys', 'corrupt')])
def test_bad_sidecar_refuses_restore_before_touching_destination(env, tmp_path, component, damage):
    out, _ = snapshot(env, tmp_path)
    file = next(Path(str(out) + '.' + component).iterdir())
    if damage == 'missing':
        file.unlink()
    else:
        file.write_bytes(b'corrupt')
    live, previous_secret = destination(tmp_path)
    before = live.read_bytes()
    existing = set(root_for(live).iterdir())
    with pytest.raises(backup.BackupError, match='Managed SSH credential backup'):
        backup.restore_backup(out, live)
    assert live.read_bytes() == before
    assert set(root_for(live).iterdir()) == existing
    assert _load_secret(str(previous_secret)) == b'existing-destination-secret'
    assert not list(live.parent.glob('*.restore-incoming*'))


@pytest.mark.parametrize('component', ['key', 'ciphertext'])
def test_backup_missing_source_dependency_does_not_publish_success(env, tmp_path, component):
    assert provision(env).status_code == 201
    original = ref(env)
    (key_path(original.parent) if component == 'key' else original).unlink()
    out = tmp_path / 'failed.db'
    with pytest.raises(backup.BackupError, match='Managed SSH credential backup incomplete'):
        backup.create_backup(env[3], out)
    assert not out.exists()
    assert not Path(str(out) + '.meta.json').exists()


def test_missing_destination_key_does_not_replace_it_or_live_db(env, tmp_path):
    out, _ = snapshot(env, tmp_path)
    live, previous_secret = destination(tmp_path)
    key_path(root_for(live)).unlink()
    before = live.read_bytes()
    with pytest.raises(backup.BackupError, match='Restore failed'):
        backup.restore_backup(out, live)
    assert live.read_bytes() == before
    assert not key_path(root_for(live)).exists()
    assert set(root_for(live).iterdir()) == {previous_secret}
    assert not list(live.parent.glob('*.restore-incoming*'))


def test_failed_database_swap_restores_previous_db_and_cleans_new_secrets(env, tmp_path, monkeypatch):
    out, _ = snapshot(env, tmp_path)
    live, previous_secret = destination(tmp_path)
    before = live.read_bytes()
    key_before = key_path(root_for(live)).read_bytes()
    real_replace = os.replace
    def fail_install(source, target):
        if str(source).endswith('.restore-incoming') and Path(target) == live:
            raise OSError('simulated install failure')
        return real_replace(source, target)
    monkeypatch.setattr(backup.os, 'replace', fail_install)
    with pytest.raises(backup.BackupError, match='Restore failed'):
        backup.restore_backup(out, live)
    assert live.read_bytes() == before
    assert key_path(root_for(live)).read_bytes() == key_before
    assert set(root_for(live).iterdir()) == {previous_secret}
    assert _load_secret(str(previous_secret)) == b'existing-destination-secret'
    assert not list(live.parent.glob('*.restore-incoming*'))


def test_partial_reencryption_failure_removes_new_ciphertexts(env, tmp_path, monkeypatch):
    assert provision(env, id='second').status_code == 201
    out, _ = snapshot(env, tmp_path)
    live, previous_secret = destination(tmp_path)
    before = live.read_bytes()
    real_write = ConnectorSecrets.write
    calls = []
    def fail_second(store, value):
        calls.append(1)
        if len(calls) == 2:
            raise OSError('simulated disk failure')
        return real_write(store, value)
    monkeypatch.setattr(ConnectorSecrets, 'write', fail_second)
    with pytest.raises(backup.BackupError, match='Restore failed'):
        backup.restore_backup(out, live)
    assert len(calls) == 2
    assert live.read_bytes() == before
    assert set(root_for(live).iterdir()) == {previous_secret}
    assert not list(live.parent.glob('*.restore-incoming*'))
