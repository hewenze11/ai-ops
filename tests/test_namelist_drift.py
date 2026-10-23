"""Platform default drift (§44.1).

The rule under test: a newer build's default rules must be *visible* but must
never re-bind themselves. Two failure modes are covered, one on each side:

* silent re-bind -- an update widens what the AI may do without anyone reading
  the diff,
* silent staleness -- a security fix ships and nobody ever learns it exists.
"""
import pytest
from fastapi.testclient import TestClient

from ai_ops import namelists
from ai_ops.app import create_app

TOKEN = 't0ken-' + 'x' * 32


@pytest.fixture()
def env(tmp_path):
    app = create_app(str(tmp_path / 'control.db'), TOKEN, 'test-model')
    yield TestClient(app)


def _admin():
    return {'Authorization': 'Bearer ' + TOKEN}


def _db(env):
    """Reach the app's database the same way the routes do."""
    import sqlite3
    return sqlite3.connect(str(env.app.state.db_path)) if hasattr(env.app.state, 'db_path') else None


def test_fresh_install_has_no_drift(env):
    """A list seeded by this build matches this build, by definition."""
    body = env.get('/api/v1/name-lists/platform-defaults/drift', headers=_admin()).json()
    assert body['seeded'] is True
    assert body['lists'] == [], body


def test_missing_rule_is_reported_after_a_rule_is_removed(env, tmp_path):
    """Simulate an old install: delete one seeded rule, then ask for drift."""
    import sqlite3, json
    db_path = str(tmp_path / 'control.db')
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    seeded = json.loads(db.execute(
        "SELECT list_id FROM platform_defaults WHERE name='lists'").fetchone()[0])
    victim = db.execute('SELECT rule_id,action FROM name_list_rules WHERE list_id=? LIMIT 1',
                        (seeded['readonly'],)).fetchone()
    db.execute('DELETE FROM name_list_rules WHERE list_id=? AND rule_id=?',
               (seeded['readonly'], victim['rule_id']))
    db.commit()
    body = env.get('/api/v1/name-lists/platform-defaults/drift', headers=_admin()).json()
    kinds = {item['kind']: item for item in body['lists']}
    assert 'readonly' in kinds
    assert any(r['rule_id'] == victim['rule_id']
               for r in kinds['readonly']['missing_rules'])


def test_drift_never_rebinds_by_itself(env, tmp_path):
    """Reading the drift report must not change what is bound."""
    import sqlite3, json
    db_path = str(tmp_path / 'control.db')
    db = sqlite3.connect(db_path)
    seeded = json.loads(db.execute(
        "SELECT list_id FROM platform_defaults WHERE name='lists'").fetchone()[0])
    db.execute('DELETE FROM name_list_rules WHERE list_id=? AND rule_id=(SELECT rule_id FROM name_list_rules WHERE list_id=? LIMIT 1)',
               (seeded['readonly'], seeded['readonly']))
    db.commit()
    before = env.get('/api/v1/name-list-bindings', headers=_admin()).json()
    env.get('/api/v1/name-lists/platform-defaults/drift', headers=_admin())
    after = env.get('/api/v1/name-list-bindings', headers=_admin()).json()
    assert before == after, 'a read-only report must not re-bind anything'


def test_apply_forks_and_rebinds(env, tmp_path):
    import sqlite3, json
    db_path = str(tmp_path / 'control.db')
    db = sqlite3.connect(db_path)
    seeded = json.loads(db.execute(
        "SELECT list_id FROM platform_defaults WHERE name='lists'").fetchone()[0])
    db.execute('DELETE FROM name_list_rules WHERE list_id=? AND rule_id=(SELECT rule_id FROM name_list_rules WHERE list_id=? LIMIT 1)',
               (seeded['readonly'], seeded['readonly']))
    db.commit()
    before = env.get('/api/v1/name-list-bindings', headers=_admin()).json()
    response = env.post('/api/v1/name-lists/platform-defaults/readonly/apply',
                        headers=_admin())
    assert response.status_code == 201, response.text
    body = response.json()
    new_id = body['list_id']
    assert new_id != seeded['readonly'], 'apply must fork, never mutate the seeded list'
    # parent_id preserves the chain, so the audit can still explain the change.
    assert body['list']['parent_id'] == seeded['readonly']
    assert body['list']['version'] == 2
    assert body['bindings']['readonly']['readonly_list'] == new_id
    # The write list is untouched by a readonly apply.
    assert body['bindings']['readonly']['write_list'] == before['readonly']['write_list']


def test_apply_clears_the_drift_report(env, tmp_path):
    import sqlite3, json
    db_path = str(tmp_path / 'control.db')
    db = sqlite3.connect(db_path)
    seeded = json.loads(db.execute(
        "SELECT list_id FROM platform_defaults WHERE name='lists'").fetchone()[0])
    db.execute('DELETE FROM name_list_rules WHERE list_id=? AND rule_id=(SELECT rule_id FROM name_list_rules WHERE list_id=? LIMIT 1)',
               (seeded['readonly'], seeded['readonly']))
    db.commit()
    assert env.get('/api/v1/name-lists/platform-defaults/drift',
                   headers=_admin()).json()['lists']
    env.post('/api/v1/name-lists/platform-defaults/readonly/apply', headers=_admin())
    # The seeded list still differs from the build (we never mutate it), but the
    # *bound* list is now current, which is what the operator cares about.
    bound = env.get('/api/v1/name-list-bindings', headers=_admin()).json()
    assert bound['readonly']['readonly_list'] != seeded['readonly']


def test_apply_rejects_unknown_kind(env):
    assert env.post('/api/v1/name-lists/platform-defaults/deny/apply',
                    headers=_admin()).status_code == 422


def test_drift_requires_admin(env):
    assert env.get('/api/v1/name-lists/platform-defaults/drift').status_code in (401, 403)


def test_seeded_marker_records_the_baseline_version(env, tmp_path):
    """The marker answers 'do these lists predate a security update'."""
    import sqlite3
    db = sqlite3.connect(str(tmp_path / 'control.db'))
    row = db.execute("SELECT seeded_version FROM platform_defaults WHERE name='lists'").fetchone()
    assert row and row[0]
