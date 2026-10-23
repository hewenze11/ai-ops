"""Sensitive-content unlock (§51.4).

These tests are written to attack the feature, because its whole purpose is to
hold a secret the model must not see. The questions asked here are:

* can the model reach the unlock? (it must not -- no model-facing tool exists)
* can an unlock be reused, or stretched to another file? (it must not)
* does a wrong confirmation still unlock? (it must not)
* is the secret ever stored on the control plane? (it must not)
"""
import json

import pytest
from fastapi.testclient import TestClient

from ai_ops import sensitive
from ai_ops.app import create_app
from ai_ops.scrubbing import MASK


@pytest.fixture()
def env(tmp_path):
    app = create_app(str(tmp_path / 'control.db'), 't0ken-' + 'x' * 32, 'test-model')
    client = TestClient(app)
    yield client, app, tmp_path


def _admin(client):
    return {'Authorization': 'Bearer ' + 't0ken-' + 'x' * 32}


def _state(app):
    return app.state.state_path if hasattr(app.state, 'state_path') else None


def test_record_stores_rules_not_values(env):
    client, app, tmp_path = env
    import sqlite3
    db_path = tmp_path / 'control.db'
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, name TEXT)')
    hit = sensitive.record(db, turn_id='turn-1', task_id='task-1', asset_id='a1',
                           path='/etc/app/config.yaml',
                           rule_ids=['pem_private_key', 'named_secret'],
                           baseline_version=sensitive._baseline_version())
    row = db.execute('SELECT * FROM sensitive_hits WHERE id=?', (hit,)).fetchone()
    assert row['path'] == '/etc/app/config.yaml'
    assert json.loads(row['rule_ids']) == ['named_secret', 'pem_private_key']
    # Nothing in the row may be the secret itself.
    blob = ' '.join(str(v) for v in tuple(row))
    assert 'BEGIN PRIVATE KEY' not in blob
    assert row['reveal_state'] == 'masked'


def test_pending_lists_masked_hits_only(env):
    client, app, tmp_path = env
    import sqlite3
    db = sqlite3.connect(tmp_path / 'control.db')
    db.row_factory = sqlite3.Row
    first = sensitive.record(db, turn_id='t1', task_id='k1', asset_id='a1',
                             path='/a', rule_ids=['r1'], baseline_version='v')
    second = sensitive.record(db, turn_id='t1', task_id='k2', asset_id='a1',
                              path='/b', rule_ids=['r2'], baseline_version='v')
    db.execute("UPDATE sensitive_hits SET reveal_state='revealed' WHERE id=?", (first,))
    db.commit()
    items = sensitive.pending(db, 't1')
    assert [item['hit_id'] for item in items] == [second]


def test_unlock_refuses_wrong_confirmation(env):
    client, app, tmp_path = env
    import sqlite3
    db = sqlite3.connect(tmp_path / 'control.db')
    db.row_factory = sqlite3.Row
    hit = sensitive.record(db, turn_id='t1', task_id='k1', asset_id='a1',
                           path='/etc/app/config.yaml', rule_ids=['r'], baseline_version='v')
    db.commit()
    response = client.post('/api/v1/sensitive-hits/%s/unlock' % hit,
                           headers=_admin(client),
                           json={'confirm_path': '/etc/passwd'})
    assert response.status_code == 422
    row = db.execute('SELECT reveal_state FROM sensitive_hits WHERE id=?', (hit,)).fetchone()
    assert row['reveal_state'] == 'masked', 'a wrong confirmation must not unlock'


def test_unlock_is_one_shot(env):
    """Full path: unlock -> dispatch -> agent re-reads -> bytes returned once.

    The reveal is not answered from what the control plane already holds: it
    holds only the masked copy, which is exactly the problem the feature
    exists to solve. So the endpoint dispatches a re-read and answers 202; the
    operator polls with the same token.
    """
    client, app, tmp_path = env
    hit, task_id, asset_id, agent_token = _seed_read(client, tmp_path)
    first = client.post('/api/v1/sensitive-hits/%s/unlock' % hit,
                        headers=_admin(client), json={})
    assert first.status_code == 200
    token = first.json()['reveal_token']
    # Second unlock attempt is refused: the grant already exists.
    assert client.post('/api/v1/sensitive-hits/%s/unlock' % hit,
                       headers=_admin(client), json={}).status_code == 409

    # First fetch dispatches the re-read instead of returning bytes.
    dispatched = client.get('/api/v1/sensitive-hits/%s/revealed' % hit,
                            headers=_admin(client), params={'token': token})
    assert dispatched.status_code == 200
    assert dispatched.json()['pending'] is True
    assert dispatched.json()['revealed'] is False
    reveal_task = dispatched.json()['task_id']

    # Polling again before the agent reports is still pending, not an error,
    # and the grant is NOT spent yet.
    assert client.get('/api/v1/sensitive-hits/%s/revealed' % hit,
                      headers=_admin(client), params={'token': token}).json()['pending'] is True

    # The agent picks it up and reports the unmasked bytes.
    assert _report_reveal(client, tmp_path, hit, 'password = hunter2-real-value', agent_token).status_code == 200
    used = client.get('/api/v1/sensitive-hits/%s/revealed' % hit,
                      headers=_admin(client), params={'token': token})
    assert used.status_code == 200
    assert used.json()['revealed'] is True
    assert 'hunter2-real-value' in used.json()['text']
    again = client.get('/api/v1/sensitive-hits/%s/revealed' % hit,
                       headers=_admin(client), params={'token': token})
    assert again.status_code == 409, 'the grant must not be reusable'


def test_reveal_bytes_never_land_in_the_task_result(env):
    """The only place unmasked bytes may live is reveal_secret.

    ``tasks.result`` is what becomes model-visible tool output. If a reveal
    wrote there, the secret would be handed to the model on the next context
    build -- silently, since nothing in that path knows about reveals.
    """
    client, app, tmp_path = env
    hit, _task_id, asset_id, agent_token = _seed_read(client, tmp_path)
    token = client.post('/api/v1/sensitive-hits/%s/unlock' % hit,
                        headers=_admin(client), json={}).json()['reveal_token']
    reveal_task = client.get('/api/v1/sensitive-hits/%s/revealed' % hit,
                             headers=_admin(client), params={'token': token}).json()['task_id']
    secret = 'sk-live-abcdefghijklmnopqrstuvwxyz'
    assert _report_reveal(client, tmp_path, hit, secret, agent_token).status_code == 200

    stored = None
    db = _db(tmp_path)
    try:
        stored = db.execute('SELECT result FROM tasks WHERE id=?', (reveal_task,)).fetchone()[0]
    finally:
        db.close()
    assert secret not in (stored or ''), 'unmasked bytes reached tasks.result'
    assert 'operator view only' in json.loads(stored)['stdout']
    # And the real copy lives where only the operator endpoint reads.
    stash = None
    db = _db(tmp_path)
    try:
        stash = db.execute('SELECT stdout FROM reveal_secret WHERE hit_id=?', (hit,)).fetchone()
    finally:
        db.close()
    assert stash is not None and secret in stash[0]
    # Returning it deletes the stashed copy: this is not an archive.
    client.get('/api/v1/sensitive-hits/%s/revealed' % hit,
               headers=_admin(client), params={'token': token})
    fresh = _db(tmp_path)
    try:
        assert fresh.execute('SELECT COUNT(*) FROM reveal_secret WHERE hit_id=?', (hit,)).fetchone()[0] == 0
    finally:
        fresh.close()


def _seed_read(client, tmp_path):
    """A completed masked read plus a hit, shaped like the real flow.

    Uses the app's own schema (created by ``create_app``) rather than a
    hand-rolled one, so this test cannot drift away from the real tables. Opens
    and closes its own connection: holding one open would block the app.
    """
    asset_id = 'a1'
    # Register through the API: the agent token is hashed on the way in, so a
    # hand-written row cannot authenticate and the test would fail confusingly.
    agent_token = client.post('/api/v1/assets', headers=_admin(client),
                              json={'id': asset_id, 'name': 'host',
                                    'allowed_users': ['nobody']}).json().get('agent_token', '')
    db = _db(tmp_path)
    try:
        task_id = 'read-task-1'
        payload = {'kind': 'readonly', 'tool': 'read_file', 'program': '/usr/bin/cat',
                   'params': {'path': '/etc/app/config.yaml'}, 'run_as': 'nobody',
                   'execution_users': ['nobody'], 'asset_id': asset_id, 'mode': 'readonly',
                   'role_id': 'ops'}
        db.execute('INSERT OR REPLACE INTO tasks(id,role_id,asset_id,payload,state,idempotency_key,created_at,updated_at,turn_id)'
                   ' VALUES(?,?,?,?,?,?,?,?,?)',
                   (task_id, 'ops', asset_id, json.dumps(payload), 'succeeded', 'read:1', 0, 0, 't1'))
        hit = sensitive.record(db, turn_id='t1', task_id=task_id, asset_id=asset_id,
                               path='/etc/app/config.yaml', rule_ids=['named_secret'],
                               baseline_version='v')
        db.commit()
    finally:
        db.close()
    # Create the role through the API so this fixture never has to guess the
    # schema; a hand-rolled INSERT drifts every time a column is added.
    client.post('/api/v1/roles', headers=_admin(client), json={'id': 'ops', 'name': 'ops'})
    return hit, task_id, asset_id, agent_token


def _db(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / 'control.db', timeout=15)
    db.row_factory = sqlite3.Row
    return db


def _report_reveal(client, tmp_path, hit_id, text, agent_token):
    """Claim the queued reveal task and report unmasked output, as an agent would."""
    db = _db(tmp_path)
    try:
        row = db.execute("SELECT * FROM tasks WHERE state='queued' AND idempotency_key=?",
                         ('reveal:' + hit_id,)).fetchone()
        assert row is not None, 'no reveal task was queued'
        task_id, asset_id = row['id'], row['asset_id']
        claim_id = 'c' * 32
        db.execute("UPDATE tasks SET state='claimed',claim_id=? WHERE id=?", (claim_id, task_id))
        db.commit()
    finally:
        db.close()
    return client.post('/api/v1/agents/%s/tasks/%s/result' % (asset_id, task_id),
                       headers={'Authorization': 'Bearer ' + agent_token},
                       json={'claim_id': claim_id, 'status': 'succeeded', 'exit_code': 0,
                             'stdout': text, 'stderr': ''})


def test_reveal_rejects_wrong_token(env):
    client, app, tmp_path = env
    import sqlite3
    db = sqlite3.connect(tmp_path / 'control.db')
    db.row_factory = sqlite3.Row
    hit = sensitive.record(db, turn_id='t1', task_id='k1', asset_id='a1',
                           path='/x', rule_ids=['r'], baseline_version='v')
    db.commit()
    client.post('/api/v1/sensitive-hits/%s/unlock' % hit, headers=_admin(client), json={})
    bad = client.get('/api/v1/sensitive-hits/%s/revealed' % hit,
                     headers=_admin(client), params={'token': 'nope'})
    assert bad.status_code == 403


def test_reveal_without_unlock_is_refused(env):
    client, app, tmp_path = env
    import sqlite3
    db = sqlite3.connect(tmp_path / 'control.db')
    db.row_factory = sqlite3.Row
    hit = sensitive.record(db, turn_id='t1', task_id='k1', asset_id='a1',
                           path='/x', rule_ids=['r'], baseline_version='v')
    db.commit()
    assert client.get('/api/v1/sensitive-hits/%s/revealed' % hit,
                      headers=_admin(client), params={'token': ''}).status_code == 409


def test_unlock_requires_admin(env):
    client, app, tmp_path = env
    import sqlite3
    db = sqlite3.connect(tmp_path / 'control.db')
    db.row_factory = sqlite3.Row
    hit = sensitive.record(db, turn_id='t1', task_id='k1', asset_id='a1',
                           path='/x', rule_ids=['r'], baseline_version='v')
    db.commit()
    assert client.post('/api/v1/sensitive-hits/%s/unlock' % hit, json={}).status_code in (401, 403)


def test_no_model_facing_unlock_tool():
    """The decisive property: if the model had an unlock tool, masking would be
    advisory. Tools live in the readonly/action surfaces, and neither may name
    an unlock, reveal or sensitive verb."""
    from ai_ops import actions, readonly_tools
    names = set(readonly_tools.PROGRAMS) | set(actions.ACTIONS)
    for name in names:
        lowered = name.lower()
        assert 'unlock' not in lowered, name
        assert 'reveal' not in lowered, name
        assert 'sensitive' not in lowered, name


def test_model_notice_names_rules_and_path_but_no_value():
    notice = sensitive.model_notice(['pem_private_key'], '/etc/app/key.pem')
    assert notice['sensitive_content_masked'] is True
    assert notice['rule_ids'] == ['pem_private_key']
    assert notice['path'] == '/etc/app/key.pem'
    # The only textual field is a fixed sentence, never the secret.
    assert notice['note'].startswith('Masked as potentially secret')


def test_expired_grant_is_refused(env, monkeypatch):
    client, app, tmp_path = env
    import sqlite3
    db = sqlite3.connect(tmp_path / 'control.db')
    db.row_factory = sqlite3.Row
    hit = sensitive.record(db, turn_id='t1', task_id='k1', asset_id='a1',
                           path='/x', rule_ids=['r'], baseline_version='v')
    db.commit()
    token = client.post('/api/v1/sensitive-hits/%s/unlock' % hit,
                        headers=_admin(client), json={}).json()['reveal_token']
    # Wind the clock forward past the TTL.
    db.execute('UPDATE sensitive_hits SET revealed_at=? WHERE id=?',
               (__import__('time').time() - sensitive.UNLOCK_TTL_SECONDS - 10, hit))
    db.commit()
    response = client.get('/api/v1/sensitive-hits/%s/revealed' % hit,
                          headers=_admin(client), params={'token': token})
    assert response.status_code == 410
