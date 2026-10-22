import json
import time
from unittest.mock import patch
from fastapi.testclient import TestClient
from ai_ops.app import create_app

ADMIN = {'Authorization': 'Bearer test-admin-token-01234567890123456789'}

def client(tmp_path):
    app = create_app(str(tmp_path/'test.db'), 'test-admin-token-01234567890123456789', default_model='default-test')
    return app, TestClient(app, headers=ADMIN)


def test_generated_ids_atomic_model_and_compatibility(tmp_path):
    app, c = client(tmp_path)
    role = c.post('/api/v1/console/roles', json={'name': '角色', 'model': {'enabled': True}})
    assert role.status_code == 201
    rid = role.json()['id']
    assert rid.startswith('role-')
    assert c.get(f'/api/v1/roles/{rid}/model').json()['enabled']
    for route, body, key in [('/api/v1/assets', {'name':'资产','allowed_users':['reader']},'asset_id'),
                             ('/api/v1/skills', {'name':'Skill','content':'text'},'id'),
                             ('/api/v1/documents', {'name':'文档','content':'text'},'id'),
                             ('/api/v1/custom-tasks', {'name':'任务','kind':'trigger','mode':'confirm','role_id':rid,'prompt':'test','execution_grants':[]},'id')]:
        one, two = c.post(route,json=body), c.post(route,json=body)
        assert one.status_code == two.status_code == 201, one.text
        assert one.json()[key] != two.json()[key]
        if route in ('/api/v1/skills','/api/v1/documents'):
            body['id'] = one.json()[key]
            assert c.post(route,json=body).status_code == 409
    assert c.post('/api/v1/console/roles',json={'id':'legacy','name':'legacy'}).status_code == 201
    assert c.post('/api/v1/console/roles',json={'id':'legacy','name':'other'}).status_code == 409
    app.state.enforce_model_readiness = True
    message={'text':'test','execution_users':[],'idempotency_key':'test-key-123'}
    assert c.post('/api/v1/roles/legacy/messages',json=message).status_code == 409
    assert c.post(f'/api/v1/roles/{rid}/messages',json=message).status_code == 409
    assert c.get('/api/v1/roles/legacy/turns').json() == []
    assert c.get('/api/v1/runtime/model').json()['provider_scope'] == 'role_or_global'


def test_true_state_history_ownership_ttl_and_logs(tmp_path):
    app,c=client(tmp_path)
    aid=c.post('/api/v1/assets',json={'name':'Agent','allowed_users':['reader']}).json()
    asset=aid['asset_id']; auth={'Authorization':'Bearer '+aid['agent_token']}
    hb={'instance_id':'00000000-0000-4000-8000-000000000001','agent_version':'test','protocol_version':'1.2','busy':False,
        'heartbeat_telemetry':{'session_id':'00000000-0000-4000-8000-000000000001','controller':'ours','attempt_seq':1,'previous_results':[]}}
    for seq,busy,owner,expected in [(1,False,None,1),(2,True,'ours',2),(3,True,'another',3),(4,True,None,None)]:
        hb.update(busy=busy,busy_by=owner);hb['heartbeat_telemetry']['attempt_seq']=seq
        assert c.post(f'/api/v1/agents/{asset}/heartbeat',json=hb,headers=auth).status_code == 200
        health=c.get('/api/v1/assets').json()[0]['health']
        assert health['execution_state'] == expected
    assert len(health['state_history']) == 4
    logs=c.get('/api/v1/logs',params={'kind':'heartbeat','asset_id':asset}).json()['items']
    assert len(logs)==4 and logs[0]['details']['state'] is None
    hb.update(busy=False,busy_by=None);hb['heartbeat_telemetry']['attempt_seq']=5
    with patch('ai_ops.execution.time.time',return_value=time.time()-40):
        c.post(f'/api/v1/agents/{asset}/heartbeat',json=hb,headers=auth)
    health=c.get('/api/v1/assets').json()[0]['health']
    assert health['execution_state']==4
    assert health['state_history'][-1]['source']=='observed_ttl_expiry'
    hb['protocol_version']='1.1';hb['heartbeat_telemetry']['attempt_seq']=6
    c.post(f'/api/v1/agents/{asset}/heartbeat',json=hb,headers=auth)
    assert c.get('/api/v1/assets').json()[0]['health']['execution_state'] is None


def test_webhook_test_is_explicit_safe_and_no_queue_mutation(tmp_path):
    app,c=client(tmp_path)
    route='/api/v1/custom-tasks/notification-test'
    assert TestClient(app).post(route,json={'url':'https://example.com'}).status_code in (401,403)
    assert c.post(route,json={'url':'http://127.0.0.1/secret'}).status_code == 422
    assert c.post(route,json={'mode':'inherit'}).status_code == 409
    with patch('ai_ops.alert_notify.post_once',return_value=204) as send:
        result=c.post(route,json={'url':'https://example.com/private?token=secret'}).json()
        assert result['http_delivered'] and result['http_status']==204 and not result['recipient_confirmed']
        assert send.call_count==1
    assert not c.get('/api/v1/runtime/notifications').json()['worker_running']
    assert c.get('/api/v1/alert-notify/deliveries').json()==[]
    assert 'token=secret' not in c.get('/api/v1/audit').text
    with patch('ai_ops.alert_notify.post_once',side_effect=RuntimeError('private-url-secret')):
        response=c.post(route,json={'url':'https://example.com'}).text
        assert 'private-url-secret' not in response
        assert 'RuntimeError' in response


def test_ssh_edit_keeps_secret_reference_and_identity(tmp_path):
    app,c=client(tmp_path)
    body={'name':'SSH','allowed_users':['reader'],'connection_type':'ssh','ssh_host':'test.example','ssh_user':'reader','ssh_auth_kind':'key','ssh_secret_ref':'/secret/key','ssh_host_key':'ssh-ed25519 public'}
    aid=c.post('/api/v1/assets',json=body).json()['asset_id']
    result=c.put(f'/api/v1/assets/{aid}/connection',json={'ssh_host':'new.example','ssh_secret_ref':''})
    assert result.status_code==200
    assert result.json()['ssh_host']=='new.example'
    assert '/secret/key' not in result.text
    with app.state.transaction() as db:
        assert db.execute('SELECT ssh_secret_ref FROM asset_connections WHERE asset_id=?',(aid,)).fetchone()[0]=='/secret/key'
