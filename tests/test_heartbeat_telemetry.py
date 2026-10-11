"""Local HTTP/SQLite only: receipt evidence is not sender ACK or packet loss."""
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from ai_ops.app import create_app
from ai_ops.heartbeat_telemetry import MAX_SEQ, Telemetry, record, view, prune
from ai_ops.observability import health_view

ADMIN = {'Authorization': 'Bearer ' + 'a'*40}

@pytest.fixture
def env(tmp_path):
    client = TestClient(create_app(str(tmp_path/'db.sqlite'), 'a'*40, 'test'))
    token = client.post('/api/v1/assets', headers=ADMIN, json={'id':'web1','name':'Web','allowed_users':['reader']}).json()['agent_token']
    return client, {'Authorization':'Bearer '+token}


def payload(seq=1, session=None, controller='ours', results=None, **kwargs):
    session = session or '00000000-0000-4000-8000-000000000001'
    return {'instance_id':session,'agent_version':'test','protocol_version':'1.2',
        'heartbeat_telemetry':{'session_id':session,'controller':controller,'attempt_seq':seq,'previous_results':results or []}, **kwargs}


def post(env, body):
    c, headers = env
    return c.post('/api/v1/agents/web1/heartbeat', headers=headers, json=body)


def status(env):
    return env[0].get('/api/v1/agents/web1/status', headers=ADMIN).json()


def test_failed_send_gap_late_receipt_ack_independent_and_short_window(env):
    assert post(env,payload(1)).status_code == 200
    assert post(env,payload(3,results=[{'attempt_seq':1,'outcome':'unconfirmed'}, {'attempt_seq':2,'outcome':'unconfirmed'}])).status_code == 200
    hb = status(env)['heartbeat']
    assert (hb['attempt_count'],hb['received_count'],hb['not_received_count']) == (3,2,1)
    assert hb['attempts'][0]['receipt']=='received' and hb['attempts'][0]['ack_status']=='unconfirmed'
    assert hb['attempts'][1]['receipt']=='not_received_yet'
    assert hb['attempts'][2]['ack_status']=='unknown'  # server cannot observe its own ACK arriving
    post(env,payload(2))
    hb = status(env)['heartbeat']
    assert (hb['through_seq'],hb['received_count']) == (3,3)
    assert hb['attempts'][1]['ack_status']=='unconfirmed'  # late receipt does not falsify timeout
    for _ in range(3):
        post(env,payload(3))
    hb = status(env)['heartbeat']
    assert (hb['attempt_count'],hb['received_count']) == (3,3)
    c = env[0]
    data = c.get('/api/v1/assets/web1/heartbeats?view=attempts',headers=ADMIN).json()
    assert len(data['items'])==3
    assert len({r['seq'] for r in data['items']})==3
    assert data['summary']['received_count']==3
    # Default cursor & coalesced window endpoint remain source-compatible.
    legacy = c.get('/api/v1/assets/web1/heartbeats',headers=ADMIN).json()
    assert legacy['view']=='windows' and all(set(r)=={'seq','asset_id','received_at'} for r in legacy['items'])


def test_ten_sequence_window_huge_gaps_are_bounded(env):
    for n in [1,2,4,5,7,8,9,10,11,12]:
        post(env,payload(n))
    h=status(env)['heartbeat']
    assert (h['from_seq'],h['through_seq'],h['attempt_count'],h['received_count'])==(3,12,10,8)
    post(env,payload(MAX_SEQ))
    h=status(env)['heartbeat']
    assert h['attempt_count']==10 and h['received_count']==1
    assert h['through_seq']==MAX_SEQ
    post(env,payload(2))  # ancient replay cannot enlarge logs or roll back cursor
    assert status(env)['heartbeat']['through_seq']==MAX_SEQ
    with env[0].app.state.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM heartbeat_attempts').fetchone()[0]==1


def test_restart_delayed_old_session_and_two_controllers(env):
    one=payload()['instance_id']; two=str(uuid.uuid4())
    post(env,payload(10,session=one))
    post(env,payload(1,session=two,busy=True,busy_by='other',busy_task='task'))
    post(env,payload(11,session=one))  # old session must not replace new presence
    s=status(env)
    assert s['presence']['instance_id']==two and s['busy_by']=='other'
    assert s['heartbeat']['session_id']==two and s['heartbeat']['attempt_count']==1
    post(env,payload(4,session=two,controller='second'))
    streams={s['controller']:s for s in status(env)['heartbeat']['streams']}
    assert streams['ours']['through_seq']==1 and streams['second']['through_seq']==4
    assert streams['ours']['received_count']==streams['second']['received_count']==1
    # Older sequence in current stream repairs evidence but cannot roll back busy.
    post(env,payload(8,session=two,controller='second',busy=True,busy_by='x'))
    post(env,payload(6,session=two,controller='second',busy=False))
    assert status(env)['busy_by']=='x'


def test_offline_tail_unknown_and_24h_expiry(env):
    post(env,payload(4))
    c=env[0]
    with c.app.state.transaction() as db:
        t=db.execute('SELECT updated_at FROM heartbeat_streams').fetchone()[0]
        asset=db.execute("SELECT * FROM assets WHERE id='web1'").fetchone()
        before=health_view(db,asset,c.app,t+1)['heartbeat']
        offline=health_view(db,asset,c.app,t+60)
        assert not offline['online'] and offline['heartbeat']['stale']
        h=offline['heartbeat']
        assert h['attempt_count']==4 and h['received_count']==1
        assert h['tail_attempts']=='unknown' and h['updated_at']==before['updated_at']
        assert view(db,'web1',t+86401) is None
        prune(db,t+86401)
        assert db.execute('SELECT COUNT(*) FROM heartbeat_attempts').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM heartbeat_streams').fetchone()[0]==0


def test_expired_evidence_is_unknown_not_failure(env):
    with env[0].app.state.transaction() as db:
        record(db,'web1',Telemetry(**payload(1)['heartbeat_telemetry']),100)
        record(db,'web1',Telemetry(**payload(2)['heartbeat_telemetry']),200)
        record(db,'web1',Telemetry(**payload(3)['heartbeat_telemetry']),86501)
        h=view(db,'web1',86501)
        assert h['received_count']==2 and h['not_received_count']==0
        assert h['unknown_count']==1 and h['attempts'][0]['receipt']=='unknown_expired'


def test_legacy_payload_and_legacy_restart_fallback(env):
    p=payload(); del p['heartbeat_telemetry']
    post(env,p)
    assert status(env)['heartbeat']['actual_attempts']=='unavailable'
    post(env,payload(1))
    assert status(env)['heartbeat']['metric']=='heartbeat_attempt_receipt'
    p['instance_id']='old-agent-restarted'; post(env,p)
    assert status(env)['heartbeat']['metric']=='heartbeat_window_arrival'


@pytest.mark.parametrize('mutation',[
    {'attempt_seq':-1},{'attempt_seq':0},{'attempt_seq':MAX_SEQ+1},{'attempt_seq':True},
    {'attempt_seq':'1'},{'attempt_seq':1.5},{'session_id':'not-a-session'},
    {'controller':''},{'controller':'a'*65},{'controller':'x\ny'}, {'extra':'bad'},
    {'previous_results':[{'attempt_seq':1,'outcome':'acknowledged'}]},
    {'previous_results':[{'attempt_seq':1,'outcome':'failed'}]},
    {'previous_results':[{'attempt_seq':1,'outcome':'unconfirmed'}]*11},
])
def test_invalid_telemetry_rejected_without_presence(env,mutation):
    p=payload(); p['heartbeat_telemetry'].update(mutation)
    assert post(env,p).status_code==422
    assert status(env)['presence'] is None


def test_invalid_session_mismatch_old_results_duplicates_and_auth(env):
    p=payload(20); p['heartbeat_telemetry']['session_id']=str(uuid.uuid4())
    assert post(env,p).status_code==422
    for results in [[{'attempt_seq':1,'outcome':'acknowledged'}],
                    [{'attempt_seq':19,'outcome':'acknowledged'}]*2]:
        assert post(env,payload(20,results=results)).status_code==422
    assert env[0].post('/api/v1/agents/web1/heartbeat',json=payload()).status_code==401
    assert env[0].get('/api/v1/assets/web1/heartbeats?view=attempts').status_code==401
    for query in ['view=wrong','after='+str(MAX_SEQ+1),'limit=501']:
        assert env[0].get('/api/v1/assets/web1/heartbeats?'+query,headers=ADMIN).status_code==422


def test_stream_spoofing_resource_cap_and_secret_scrub(env):
    for n in range(32):
        assert post(env,payload(controller='c'+str(n))).status_code==200
    assert post(env,payload(controller='overflow')).status_code==429
    assert len(status(env)['heartbeat']['streams'])==32
    assert post(env,payload(2,controller='c0')).status_code==200


def test_concurrent_receipts_idempotent(env):
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses=list(pool.map(lambda n:post(env,payload(n)), list(range(1,21))*2))
    assert all(r.status_code==200 for r in responses)
    h=status(env)['heartbeat']
    assert h['through_seq']==20 and h['received_count']==h['attempt_count']==10
    with env[0].app.state.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM heartbeat_attempts').fetchone()[0]==20


def test_attempt_cursor_and_ack_updates_are_snapshot_reads(env):
    post(env,payload(1)); post(env,payload(3))
    c=env[0]; url='/api/v1/assets/web1/heartbeats?view=attempts&limit=1'
    first=c.get(url,headers=ADMIN).json()
    second=c.get(url+'&after='+str(first['next_after']),headers=ADMIN).json()
    assert first['items'][0]['attempt_seq']==1 and second['items'][0]['attempt_seq']==3
    post(env,payload(4,results=[{'attempt_seq':1,'outcome':'acknowledged'}]))
    fresh=c.get(url,headers=ADMIN).json()
    assert fresh['items'][0]['ack_status']=='acknowledged'
    assert fresh['items'][0]['seq']==first['items'][0]['seq']
    post(env,payload(3,results=[{'attempt_seq':1,'outcome':'unconfirmed'}]))
    assert c.get(url,headers=ADMIN).json()['items'][0]['ack_status']=='acknowledged'


def test_telemetry_secret_shaped_identity_scrub_and_asset_purge(env):
    post(env,payload(controller='password=do-not-display'))
    c=env[0]
    assert 'do-not-display' not in c.get('/api/v1/agents/web1/status',headers=ADMIN).text
    assert 'do-not-display' not in c.get('/api/v1/assets/web1/heartbeats?view=attempts',headers=ADMIN).text
    assert c.post('/api/v1/assets/web1/retire',headers=ADMIN,json={}).status_code==200
    response=c.post('/api/v1/assets/web1/purge',headers=ADMIN,json={'confirm_asset_id':'web1'})
    assert response.status_code==200,response.text
    with c.app.state.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM heartbeat_attempts').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM heartbeat_streams').fetchone()[0]==0
