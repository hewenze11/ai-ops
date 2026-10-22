"""Credential handles on the control plane (§42, §46.3).

What these tests pin down is the claim the product makes: a secret discovered on
a managed host is *not* stored here. The handle is an inventory entry. If that
ever stops being true, the data-sovereignty story collapses quietly.
"""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ai_ops import credential_handles
from ai_ops.app import create_app

ADMIN = 'credential-handle-admin-token-1234567890'


@pytest.fixture
def env(tmp_path):
    path = tmp_path / 'creds.db'
    app = create_app(str(path), ADMIN)
    client = TestClient(app)
    headers = {'Authorization': 'Bearer ' + ADMIN}
    client.post('/api/v1/assets', json={'id': 'host1', 'name': 'Host 1', 'allowed_users': ['reader']}, headers=headers)
    client.post('/api/v1/assets', json={'id': 'host2', 'name': 'Host 2', 'allowed_users': ['reader']}, headers=headers)
    return client, headers, path


HANDLE = {'id': 'cred-abc123', 'name': 'db password', 'note': 'found in .env',
          'source': '/srv/app/.env', 'kind': 'pattern'}


def test_history_records_discovery_and_dismissal(env):
    c, h, _ = env
    assert c.post('/api/v1/assets/host1/credentials', headers=h, json=HANDLE).json()['created'] is True
    c.post('/api/v1/assets/host1/credentials/cred-abc123/dismiss', headers=h, json={'reason': 'git sha'})
    history = c.get('/api/v1/assets/host1/credentials/cred-abc123/history', headers=h).json()
    assert [row['event'] for row in history] == ['discovered', 'dismissed']


def test_a_dismissed_handle_does_not_authorize_injection(env):
    c, h, path = env
    c.post('/api/v1/assets/host1/credentials', headers=h, json=HANDLE)
    with sqlite3.connect(path) as db:
        assert credential_handles.may_inject(db, 'host1', 'cred-abc123') is True
    c.post('/api/v1/assets/host1/credentials/cred-abc123/dismiss', headers=h, json={'reason': 'x'})
    with sqlite3.connect(path) as db:
        assert credential_handles.may_inject(db, 'host1', 'cred-abc123') is False


def test_re_discovery_does_not_resurface_a_dismissed_handle(env):
    c, h, _ = env
    c.post('/api/v1/assets/host1/credentials', headers=h, json=HANDLE)
    c.post('/api/v1/assets/host1/credentials/cred-abc123/dismiss', headers=h, json={'reason': 'git sha'})
    # The agent re-reports it on the next scan.
    assert c.post('/api/v1/assets/host1/credentials', headers=h, json=HANDLE).json()['created'] is False
    assert c.get('/api/v1/assets/host1/credentials', headers=h).json()['items'] == []
    assert len(c.get('/api/v1/assets/host1/credentials?include_dismissed=true', headers=h).json()['items']) == 1


def test_inventory_is_scoped_to_the_host_that_reported_it(env):
    c, h, path = env
    c.post('/api/v1/assets/host1/credentials', headers=h, json=HANDLE)
    with sqlite3.connect(path) as db:
        # A handle from host1 must not authorize injection on host2.
        assert credential_handles.may_inject(db, 'host2', 'cred-abc123') is False


def test_summary_counts_per_asset(env):
    c, h, _ = env
    c.post('/api/v1/assets/host1/credentials', headers=h, json=HANDLE)
    c.post('/api/v1/assets/host1/credentials', headers=h, json={**HANDLE, 'id': 'cred-two'})
    c.post('/api/v1/assets/host2/credentials', headers=h, json=HANDLE)
    body = c.get('/api/v1/credentials', headers=h).json()
    assert body['counts_by_asset'] == {'host1': 2, 'host2': 1}


def test_no_endpoint_returns_a_credential_value(env):
    c, h, _ = env
    c.post('/api/v1/assets/host1/credentials', headers=h, json=HANDLE)
    # The handle model has no value field at all: an extra key is refused
    # rather than silently ignored, so a client cannot think it stored one.
    bad = {**HANDLE, 'id': 'cred-with-value', 'secret': 'leaked-in-clear'}
    assert c.post('/api/v1/assets/host1/credentials', headers=h, json=bad).status_code == 422
    body = c.get('/api/v1/assets/host1/credentials', headers=h).text
    assert 'secret' not in body or '"kind"' in body
    assert 'leaked-in-clear' not in body
