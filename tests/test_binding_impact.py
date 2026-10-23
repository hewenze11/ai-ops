"""Binding impact: who follows a re-bind (§44.3 last clause).

The distinction under test is the whole point:

* a running turn has already frozen its list ids, so a re-bind cannot change it;
* a scheduled trigger stores only a mode, so a re-bind changes what it may do
  on its next run -- silently, hours later.

Confusing those two would either raise false alarms about running work or hide
the real exposure.
"""
import pytest
from fastapi.testclient import TestClient

from ai_ops.app import create_app

TOKEN = 'bind-impact-admin-0123456789abcdef'


@pytest.fixture()
def env(tmp_path):
    app = create_app(str(tmp_path / 'control.db'), TOKEN, 'test-model')
    client = TestClient(app)
    headers = {'Authorization': 'Bearer ' + TOKEN}
    for role in ('ops', 'db'):
        assert client.post('/api/v1/roles', headers=headers,
                           json={'id': role, 'name': role}).status_code == 201
    return client, headers


def _trigger(client, headers, task_id='nightly', mode='confirm', kind='scheduled', enabled=True):
    body = {'id': task_id, 'name': task_id, 'kind': kind, 'role_id': 'ops',
            'prompt': 'Check the registered production hosts; do not guess asset identity.',
            'execution_users': ['reader'], 'mode': mode, 'enabled': enabled,
            'cron': '0 3 * * *', 'timezone': 'Asia/Shanghai'}
    response = client.post('/api/v1/custom-tasks', headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_no_triggers_means_no_warning(env):
    client, headers = env
    body = client.get('/api/v1/name-list-bindings/impact?mode=confirm', headers=headers).json()
    assert body['affected_trigger_count'] == 0
    assert body['warning'] is None


def test_scheduled_trigger_is_reported_under_its_mode(env):
    client, headers = env
    _trigger(client, headers, mode='confirm')
    body = client.get('/api/v1/name-list-bindings/impact?mode=confirm', headers=headers).json()
    assert body['affected_trigger_count'] == 1
    assert body['affected_triggers'][0]['custom_task_id'] == 'nightly'
    assert 'resolve mode' in body['warning']


def test_trigger_under_another_mode_is_not_reported(env):
    client, headers = env
    _trigger(client, headers, mode='readonly')
    body = client.get('/api/v1/name-list-bindings/impact?mode=confirm', headers=headers).json()
    assert body['affected_trigger_count'] == 0


def test_all_modes_listed_when_mode_omitted(env):
    client, headers = env
    _trigger(client, headers, mode='confirm')
    body = client.get('/api/v1/name-list-bindings/impact', headers=headers).json()
    modes = {item['mode']: item for item in body['modes']}
    assert set(modes) == {'readonly', 'confirm', 'direct'}
    assert modes['confirm']['affected_trigger_count'] == 1


def test_binding_age_is_reported(env):
    """The operator needs to know how long ago the binding changed to judge
    whether a stale trigger has already been running under the new rules."""
    client, headers = env
    body = client.get('/api/v1/name-list-bindings/impact?mode=confirm', headers=headers).json()
    assert body['binding_updated_at'] is not None
    assert body['binding_age_days'] is not None
    assert body['binding_age_days'] >= 0


def test_running_turns_are_counted_separately(env):
    """Running work is listed as unaffected, not as exposure."""
    client, headers = env
    body = client.get('/api/v1/name-list-bindings/impact?mode=confirm', headers=headers).json()
    assert 'running_turns_unaffected' in body
    assert isinstance(body['running_turns_unaffected'], int)


def test_report_explains_why_triggers_follow_the_binding(env):
    client, headers = env
    body = client.get('/api/v1/name-list-bindings/impact?mode=confirm', headers=headers).json()
    assert 'mode, not a list id' in body['note']


def test_impact_requires_admin(env):
    client, headers = env
    assert client.get('/api/v1/name-list-bindings/impact').status_code in (401, 403)
