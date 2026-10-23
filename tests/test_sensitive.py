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
    client, app, tmp_path = env
    import sqlite3
    db = sqlite3.connect(tmp_path / 'control.db')
    db.row_factory = sqlite3.Row
    hit = sensitive.record(db, turn_id='t1', task_id='k1', asset_id='a1',
                           path='/etc/app/config.yaml', rule_ids=['r'], baseline_version='v')
    db.commit()
    first = client.post('/api/v1/sensitive-hits/%s/unlock' % hit,
                        headers=_admin(client), json={})
    assert first.status_code == 200
    token = first.json()['reveal_token']
    # Second unlock attempt is refused: the grant already exists.
    assert client.post('/api/v1/sensitive-hits/%s/unlock' % hit,
                       headers=_admin(client), json={}).status_code == 409
    # Consuming it burns it.
    used = client.get('/api/v1/sensitive-hits/%s/revealed' % hit,
                      headers=_admin(client), params={'token': token})
    assert used.status_code == 200
    again = client.get('/api/v1/sensitive-hits/%s/revealed' % hit,
                       headers=_admin(client), params={'token': token})
    assert again.status_code == 409, 'the grant must not be reusable'


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
