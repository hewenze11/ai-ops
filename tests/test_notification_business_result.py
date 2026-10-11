"""Notification test response contract; no network or worker activation."""
import json
import urllib.error
from contextlib import closing
import sqlite3

import pytest
from ai_ops import alert_notify
from test_control import env


@pytest.mark.parametrize('channel,raw,accepted', [
    ('feishu', b'{"code":0}', True),
    ('feishu', b'{"StatusCode":0}', True),
    ('feishu', b'{"code":19001}', False),
    ('feishu', b'not-json', False),
    ('dingtalk', b'{"errcode":0}', True),
    ('dingtalk', b'{"errcode":310000}', False),
    ('wecom', b'{"errcode":0}', True),
    ('wecom', b'{"errcode":93000}', False),
    ('slack', b'ok', True),
    ('slack', b'invalid_payload', False),
    ('generic', b'{"unknown":"receiver-contract"}', None),
    ('discord', b'', None),
], ids=['feishu-ok','feishu-legacy-ok','feishu-rejected','invalid-json',
        'dingtalk-ok','dingtalk-rejected','wecom-ok','wecom-rejected',
        'slack-ok','slack-rejected','generic-unknown','discord-http-only'])
def test_endpoint_distinguishes_business_acceptance(env, monkeypatch, channel, raw, accepted):
    c, h, _, path = env
    calls = []
    def fake_post(url, selected, title, body):
        calls.append(selected)
        alert_notify.check_business_response(selected, raw, 200)
        return 200
    monkeypatch.setattr(alert_notify, 'post_once', fake_post)
    response = c.post('/api/v1/custom-tasks/notification-test', headers=h,
                      json={'url':'https://fixture.example/private-token', 'channel':channel})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['http_delivered'] is True and result['http_status'] == 200
    assert result['business_accepted'] is accepted
    assert not result.get('recipient_confirmed')
    assert calls == [channel]
    assert 'private-token' not in response.text
    if accepted is False:
        assert result['error'] == 'WEBHOOK_BUSINESS_REJECTED'
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('SELECT count(*) FROM custom_tasks').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
        assert all('private-token' not in str(row) for row in db.execute('SELECT * FROM audit'))


@pytest.mark.parametrize('failure,expected_status', [
    (urllib.error.HTTPError('https://fixture.example/private-token',503,'unavailable',{},None),503),
    (TimeoutError('do not echo private-token'),None),
], ids=['http503','timeout'])
def test_transport_failure_is_not_business_success(env, monkeypatch, failure, expected_status):
    c,h,_,_=env
    def fake_post(*args):
        raise failure
    monkeypatch.setattr(alert_notify,'post_once',fake_post)
    response=c.post('/api/v1/custom-tasks/notification-test', headers=h,
                    json={'url':'https://fixture.example/private-token','channel':'generic'})
    assert response.status_code == 200
    result=response.json()
    assert result['http_delivered'] is False
    assert result['http_status'] == expected_status
    assert result.get('business_accepted') is not True
    assert 'private-token' not in response.text
