import json
import time
import pytest
from fastapi.testclient import TestClient
from ai_ops.app import create_app
from ai_ops.observability import heartbeat_sample, health_view
from ai_ops.alert_notify import collect_conclusions
from ai_ops.connector_local import start_local_connector_workers, LocalExecutor
from ai_ops.connector_ssh import ConnectorExecutor
from test_modes import FakeModel, call_tool, setup_role_with_asset, send

H = {'Authorization': 'Bearer ' + 'a' * 40}

@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(str(tmp_path / 'db.sqlite'), 'a' * 40, 'test'))


def provision(c):
    c.post('/api/v1/roles', headers=H, json={'id':'ops','name':'Ops'})
    return c.post('/api/v1/assets', headers=H, json={'id':'web1','name':'Web','allowed_users':['ops_read']}).json()['agent_token']


def task(c, mode='direct'):
    return c.post('/api/v1/tasks', headers=H, json={'role_id':'ops','asset_id':'web1','execution_users':['ops_read'],'run_as':'ops_read','command':'echo hi','mode':mode,'idempotency_key':'task-00001'}).json()


def test_heartbeat_slots_retention_and_queue_evidence(client):
    token = provision(client)
    ah = {'Authorization':'Bearer '+token}
    with client.app.state.transaction() as db:
        for i in (0, 1, 3, 4, 6, 7, 8, 9):
            heartbeat_sample(db, 'web1', 1000+i*10+1)
            heartbeat_sample(db, 'web1', 1000+i*10+2)
        row = db.execute("SELECT * FROM assets WHERE id='web1'").fetchone()
        h = health_view(db,row,client.app,1100)['heartbeat']
        assert h['observed_slots'] == 10 and h['successful_slots'] == 8
        assert h['missed_ratio'] == .2
        heartbeat_sample(db,'web1',100000)
        assert db.execute('SELECT COUNT(*) FROM heartbeat_samples').fetchone()[0] == 1
    task(client)
    client.post('/api/v1/agents/web1/heartbeat', headers=ah, json={'instance_id':'i','agent_version':'v','protocol_version':'1.2','busy':True,'busy_by':'other','busy_task':'x'})
    # No local controller identity observed yet: do not invent waiting status.
    assert client.get('/api/v1/agents/web1/status',headers=H).json()['queue_state'] == 'busy'
    client.post('/api/v1/agents/web1/claim', headers=ah, json={'protocol_version':'1.2','controller':'ours'})
    status = client.get('/api/v1/agents/web1/status',headers=H).json()
    assert status['queue_state'] == 'waiting_other_controller'
    assert status['busy_by'] == 'other'
    assert status['heartbeat']['actual_attempts'] == 'unavailable'
    assert status['heartbeat']['expected_interval_seconds'] is None
    assert status['heartbeat']['observation_window_seconds'] == 10
    with client.app.state.transaction() as db:
        db.execute('UPDATE controller_observations SET observed_at=?', (time.time()-3600,))
    # Claim identity one hour old remains valid while busy heartbeat is fresh.
    assert client.get('/api/v1/agents/web1/status',headers=H).json()['queue_state'] == 'waiting_other_controller'
    with client.app.state.transaction() as db:
        db.execute('UPDATE agent_presence SET last_seen=?', (time.time()-31,))
    assert client.get('/api/v1/agents/web1/status',headers=H).json()['queue_state'] == 'offline'
    assert client.get('/api/v1/assets/web1/heartbeats?limit=501',headers=H).status_code == 422


def test_local_worker_health_not_fake_heartbeat(client):
    local = client.get('/api/v1/local-connector',headers=H).json()['asset_id']
    status = client.get('/api/v1/agents/'+local+'/status',headers=H).json()
    assert status['health_source'] == 'local_worker' and not status['online']
    stop, thread = start_local_connector_workers(client.app,client.app.state.transaction,client.app.state.audit)
    try:
        status = client.get('/api/v1/agents/'+local+'/status',headers=H).json()
        assert status['online'] and status['presence'] is None
        assert status['heartbeat']['applicable'] is False
        assert status['execution_scope'] == 'service_runtime'
        assert 'Docker' in status['scope_notice']
        assert '不代表宿主机' in status['health_notice']
    finally:
        stop.set()
        thread.join(3)


def test_inventory_is_filtered_live_and_readonly(client):
    provision(client)
    path='/api/v1/documents/system-asset-inventory'
    result=client.get(path+'?execution_users=ops_read',headers=H).json()
    assert [a['id'] for a in result['assets']] == ['web1']
    assert 'token' not in result['content']
    assert client.get(path+'?execution_users=absent',headers=H).json()['assets'] == []
    assert client.put(path,headers=H,json={'id':'system-asset-inventory','name':'x','content':'fake'}).status_code == 409
    assert client.delete(path,headers=H).status_code == 409
    client.post('/api/v1/assets/web1/retire',headers=H,json={})
    assert client.get(path+'?execution_users=ops_read',headers=H).json()['assets'] == []


def test_unified_logs_auth_paging_and_scrubbing(client):
    provision(client)
    t=task(client)
    with client.app.state.transaction() as db:
        db.execute('UPDATE tasks SET result=? WHERE id=?',(json.dumps({'stdout':'password=supersecret','stderr':'','exit_code':0,'status':'succeeded'}),t['id']))
    assert client.get('/api/v1/logs').status_code == 401
    assert client.get('/api/v1/logs?limit=201',headers=H).status_code == 422
    assert client.get('/api/v1/logs?before=bad',headers=H).status_code == 422
    first=client.get('/api/v1/logs?limit=2',headers=H).json()
    second=client.get('/api/v1/logs',params={'limit':2,'before':first['next_before']},headers=H).json()
    assert not ({r['id'] for r in first['items']} & {r['id'] for r in second['items']})
    output=client.get('/api/v1/logs?kind=output&role_id=ops',headers=H).json()
    assert 'supersecret' not in json.dumps(output)
    assert client.get('/api/v1/logs?asset_id=absent',headers=H).json()['items'] == []


def test_unknown_stored_mode_fails_closed(client):
    setup_role_with_asset(client,H)
    tid=send(client,H,'direct')['turn_id']
    with client.app.state.transaction() as db:
        db.execute("UPDATE role_turns SET mode='corrupt' WHERE id=?",(tid,))
    client.app.state.role_engine.advance(FakeModel([call_tool('execute_command',{'asset_id':'web1','run_as':'ops_read','command':'echo hi'})]))
    result=client.get('/api/v1/turns/'+tid,headers=H).json()
    assert result['state']=='failed' and result['pending_task_id'] is None


@pytest.mark.parametrize('transport',['agent','ssh','local'])
def test_account_revocation_is_rechecked_by_every_transport(client,transport):
    token=provision(client)
    task(client)
    with client.app.state.transaction() as db:
        db.execute("UPDATE assets SET allowed_users='[]' WHERE id='web1'")
    if transport=='agent':
        assert client.post('/api/v1/agents/web1/claim',headers={'Authorization':'Bearer '+token},json={'protocol_version':'1.2'}).status_code==403
    else:
        executor=(ConnectorExecutor if transport=='ssh' else LocalExecutor)(client.app.state.transaction,client.app.state.audit)
        assert executor.claim('web1') is None


def test_task_notification_result_only_redacted_and_idempotent(client,monkeypatch):
    provision(client)
    monkeypatch.setattr('ai_ops.alert_notify.validate_url',lambda url: None)
    cfg={'id':'alert','name':'Alert','kind':'trigger','role_id':'ops','prompt':'inspect','execution_users':['ops_read'],'mode':'readonly',
         'notification':{'enabled':True,'url':'https://hooks.example.com/secret-key','channel':'generic'}}
    response=client.post('/api/v1/custom-tasks',headers=H,json=cfg)
    assert response.status_code==201,response.text
    saved=response.json()
    assert 'secret-key' not in saved['notification']['url']
    # Unrelated legacy-style edit preserves notification without needing secret retrieval.
    edit={k:v for k,v in cfg.items() if k!='notification'}
    assert client.put('/api/v1/custom-tasks/alert',headers=H,json=edit).status_code==200
    invoked=client.post('/api/v1/triggers/alert/invoke',headers={'Authorization':'Bearer '+saved['trigger_token']},json={'status':'firing'}).json()
    assert collect_conclusions(client.app.state.transaction,client.app.state.audit)==0
    with client.app.state.transaction() as db:
        db.execute("UPDATE role_turns SET state='completed',final_text='diagnosed' WHERE id=?",(invoked['turn_id'],))
    assert collect_conclusions(client.app.state.transaction,client.app.state.audit)==1
    assert collect_conclusions(client.app.state.transaction,client.app.state.audit)==0
    events=client.get('/api/v1/custom-task-events',headers=H).json()
    assert 'secret-key' not in json.dumps(events)
    with client.app.state.transaction() as db:
        row=db.execute('SELECT * FROM alert_notifications').fetchone()
        assert row['url']==cfg['notification']['url'] and 'diagnosed' in row['body']


def test_windows_local_refuses_impersonation(monkeypatch):
    import ai_ops.connector_local as local
    monkeypatch.setattr(local.os, 'name', 'nt')
    monkeypatch.setattr('getpass.getuser', lambda: 'service_user')
    assert LocalExecutor._build_argv('another_user', 'echo no') is None
    assert LocalExecutor._build_argv('service_user', 'echo yes')[-1] == 'echo yes'


def test_fetch_credentials_rejected_without_network():
    from ai_ops.search import fetch_page, SearchFailure
    with pytest.raises(SearchFailure, match='FETCH_URL_INVALID'):
        fetch_page('https://user:password@example.com/')


def test_core_inventory_is_not_a_role_asset_acl(client):
    from ai_ops.context import build_messages
    setup_role_with_asset(client,H)
    tid=client.post('/api/v1/roles/ops/messages',headers=H,json={'text':'inventory','execution_users':[], 'mode':'readonly','idempotency_key':'core-scope-test'}).json()['turn_id']
    with client.app.state.transaction() as db:
        turn=db.execute('SELECT * FROM role_turns WHERE id=?',(tid,)).fetchone()
        text=build_messages(client.app,db,turn)[0]['content']
    assert '"id": "web1"' in text
    assert '"available_selected_users": []' in text


def test_attention_includes_old_and_more_than_100_standalone_approvals(client):
    provision(client)
    first=task(client, 'confirm')
    with client.app.state.transaction() as db:
        # More than one recent-history page, with no current UI turn linkage.
        for i in range(105):
            db.execute('INSERT INTO tasks(id,role_id,asset_id,payload,state,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                       ('approval-'+str(i),'ops','web1',json.dumps({'command':'password=hidden-value','run_as':'ops_read'}),'awaiting_approval','approval-key-'+str(i),time.time()+i,time.time()+i))
    assert client.get('/api/v1/operator/attention').status_code == 401
    result=client.get('/api/v1/operator/attention',headers=H).json()
    assert len(result['awaiting_approvals']) == 106
    assert result['awaiting_approvals'][0]['id'] == first['id']
    assert 'hidden-value' not in json.dumps(result['awaiting_approvals'])
    assert isinstance(result['awaiting_approvals'][0]['payload'],dict)
    assert client.post('/api/v1/tasks/'+first['id']+'/approve',headers=H).status_code == 200
    remaining=client.get('/api/v1/operator/attention',headers=H).json()['awaiting_approvals']
    assert len(remaining) == 105 and all(r['id'] != first['id'] for r in remaining)
