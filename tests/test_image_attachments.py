"""End-to-end image contract with local PNGs and a fake vision provider."""
import base64
import io
import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from ai_ops.app import create_app
from ai_ops.backup import BackupError, create_backup, verify_backup, restore_backup
from test_turns import env, FakeModel, final, get


def png(color='red'):
    out = io.BytesIO()
    Image.new('RGB', (32, 16), color).save(out, 'PNG')
    return out.getvalue()


def upload(env, role='ops', color='red'):
    return env[0].post('/api/v1/attachments?role_id=' + role, headers={**env[1], 'Content-Type': 'image/png'}, content=png(color))


def message(env, ids, key='image-message-01', text='Read this screenshot'):
    return env[0].post('/api/v1/roles/ops/messages', headers=env[1], json={
        'text': text, 'attachment_ids': ids, 'execution_grants': [], 'mode': 'readonly', 'idempotency_key': key})


def enable_vision(env):
    r = env[0].put('/api/v1/roles/ops/model', headers=env[1], json={'enabled': True, 'vision_enabled': True})
    assert r.status_code == 200, r.text


def test_upload_auth_scope_metadata_and_normalization(env):
    c, h, engine, _, _ = env
    assert c.post('/api/v1/attachments?role_id=ops', headers={'Content-Type': 'image/png'}, content=png()).status_code in (401, 403)
    created = upload(env)
    assert created.status_code == 201, created.text
    a = created.json()
    assert (a['width'], a['height'], a['mime']) == (32, 16, 'image/png')
    assert not a['duplicate'] and 'digest' not in a and 'path' not in a
    assert upload(env).json()['id'] == a['id']
    assert upload(env).json()['duplicate']
    assert upload(env, role='other').json()['id'] != a['id']
    url = '/api/v1/attachments/' + a['id']
    assert c.get(url + '?role_id=ops').status_code in (401, 403)
    assert c.get(url + '?role_id=other', headers=h).status_code == 403
    full = c.get(url + '?role_id=ops', headers=h)
    assert full.status_code == 200 and full.headers['cache-control'] == 'no-store'
    assert Image.open(io.BytesIO(full.content)).size == (32, 16)
    assert c.get(url + '?role_id=ops&thumbnail=true', headers=h).status_code == 200
    assert base64.b64encode(full.content).decode() not in c.get('/api/v1/audit', headers=h).text


@pytest.mark.parametrize('mime,data,status', [('text/plain', b'hi', 415), ('image/png', b'not an image', 422), ('image/png', b'x' * (8 * 1024 * 1024 + 1), 413)], ids=['wrong-mime', 'invalid-signature', 'over-size-limit'])
def test_invalid_image_rejected(env, mime, data, status):
    r = env[0].post('/api/v1/attachments?role_id=ops', headers={**env[1], 'Content-Type': mime}, content=data)
    assert r.status_code == status
    with env[2].transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM image_attachments').fetchone()[0] == 0


def test_message_requires_vision_and_matching_role(env):
    a = upload(env).json()['id']
    assert message(env, [a]).status_code == 409
    enable_vision(env)
    foreign = upload(env, 'other').json()['id']
    assert message(env, [foreign]).status_code == 403
    assert message(env, [a, a]).status_code == 422
    assert message(env, [], text=' ').status_code == 422
    assert message(env, [a], text='').status_code == 202


def test_real_parts_and_no_binary_duplication_in_database(env):
    enable_vision(env)
    a = upload(env).json()['id']
    first = message(env, [a])
    assert first.status_code == 202
    tid = first.json()['turn_id']
    assert message(env, [a]).json()['duplicate']
    assert message(env, []).status_code == 409
    model = FakeModel([final('Screenshot understood')])
    assert env[2].advance(model)
    parts = model.requests[0]['messages'][-1]['content']
    assert parts[0]['type'] == 'text'
    assert parts[1]['type'] == 'image_url'
    assert parts[1]['image_url']['url'].startswith('data:image/png;base64,')
    assert Image.open(io.BytesIO(base64.b64decode(parts[1]['image_url']['url'].split(',')[1]))).size == (32, 16)
    t = get(env, tid)
    assert t['state'] == 'completed' and [x['id'] for x in t['attachments']] == [a]
    with env[2].transaction() as db:
        persisted = db.execute('SELECT request FROM model_calls WHERE turn_id=?', (tid,)).fetchone()[0]
        assert 'data:image' not in persisted and 'image_reference' in persisted
        assert 'data:image' not in str(db.execute('SELECT * FROM role_memory').fetchall())


def test_disabling_vision_after_enqueue_fails_explicitly(env):
    enable_vision(env)
    tid = message(env, [upload(env).json()['id']]).json()['turn_id']
    env[0].put('/api/v1/roles/ops/model', headers=env[1], json={'enabled': True, 'vision_enabled': False})
    model = FakeModel([final()])
    assert env[2].advance(model)
    assert not model.requests
    assert get(env, tid)['error_code'] == 'ROLE_VISION_NOT_ENABLED'


def test_old_model_edits_preserve_vision_setting(env):
    enable_vision(env)
    r = env[0].put('/api/v1/roles/ops/model', headers=env[1], json={'enabled': True, 'max_output_tokens': 512})
    assert r.json()['vision_enabled'] is True


def test_storage_quota_does_not_delete_existing_images(env, monkeypatch):
    a = upload(env).json()
    monkeypatch.setenv('AI_OPS_ATTACHMENT_QUOTA_BYTES', '1')
    assert upload(env, color='blue').status_code == 413
    assert upload(env).json()['id'] == a['id']
    assert env[0].get('/api/v1/attachments/' + a['id'] + '?role_id=ops', headers=env[1]).status_code == 200


def test_backup_requires_image_files_and_restores_them(env, tmp_path):
    enable_vision(env)
    a = upload(env).json()['id']
    tid = message(env, [a]).json()['turn_id']
    backup = tmp_path / 'snapshots' / 'image.db'
    meta = create_backup(env[4], backup)
    assert meta['attachment_file_count'] == 2
    verify_backup(backup)
    target = tmp_path / 'restore' / 'restored.db'
    restore_backup(backup, target)
    from test_turns import ADMIN
    c = TestClient(create_app(str(target), ADMIN, 'test-model'), headers=env[1])
    assert c.get('/api/v1/turns/' + tid).json()['attachments'][0]['id'] == a
    assert c.get('/api/v1/attachments/' + a + '?role_id=ops').status_code == 200
    image = Path(str(backup) + '.attachments') / (a + '.png')
    image.write_bytes(b'corrupted')
    with pytest.raises(BackupError, match='attachments'):
        verify_backup(backup)
    previous = target.read_bytes()
    with pytest.raises(BackupError):
        restore_backup(backup, target)
    assert target.read_bytes() == previous
