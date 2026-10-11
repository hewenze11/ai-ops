"""Grouped audit pagination uses a fixed high-water mark and consistent date scope."""
from urllib.parse import quote
import pytest
from test_turns import env


def turn(env, key):
    result=env[0].post('/api/v1/roles/ops/messages', headers=env[1], json={
        'text':'group-'+key, 'mode':'confirm', 'execution_grants':[], 'idempotency_key':key})
    assert result.status_code==202, result.text
    return result.json()['turn_id']


def evidence(env, tid, count, stamp=1788192000):
    seqs=[]
    with env[0].app.state.transaction() as db:
        for i in range(count):
            env[0].app.state.audit(db,'fixture.evidence',tid,'admin',{'index':i,'authorization_revision':1})
            seq=db.execute('SELECT MAX(seq) FROM audit').fetchone()[0]
            db.execute('UPDATE audit SET created_at=? WHERE seq=?',(stamp,seq))
            seqs.append(seq)
    return seqs


def groups(env, **params):
    return env[0].get('/api/v1/logs/groups',headers=env[1],params=params)


def events(env, group_id, **params):
    return env[0].get('/api/v1/logs/groups/'+quote(group_id,safe='')+'/events',headers=env[1],params=params)


def test_group_events_all_pages_no_duplicates_and_frozen_highwater(env):
    tid=turn(env,'loggroup-event-001')
    expected=evidence(env,tid,121)
    result=groups(env,role_id='ops',day='2026-09-01').json()
    assert len(result['items'])==1
    group=result['items'][0]
    assert group['event_count']==121 and group['turn_id']==tid
    evidence(env,tid,1)  # New audit after snapshot must not leak into detail pages.
    page=events(env,group['id'],limit=100).json()
    assert len(page['items'])==100 and page['next_after'] is not None
    rest=events(env,group['id'],after=page['next_after'],limit=100).json()
    assert rest['next_after'] is None and len(rest['items'])==21
    assert [r['seq'] for r in page['items']+rest['items']]==expected
    assert all(r['details']['authorization_revision']==1 for r in page['items'])
    assert groups(env,role_id='ops',day='2026-09-01').json()['items'][0]['event_count']==122


def test_day_filter_matches_group_detail_scope_and_legacy_ids(env):
    from ai_ops.log_groups import encode,decode
    tid=turn(env,'loggroup-dates-001')
    first=evidence(env,tid,2,1788192000)
    second=evidence(env,tid,3,1788278400)
    group=groups(env,role_id='ops',day='2026-09-01').json()['items'][0]
    assert group['event_count']==2
    assert [r['seq'] for r in events(env,group['id']).json()['items']]==first
    group2=groups(env,role_id='ops',day='2026-09-02').json()['items'][0]
    assert group2['event_count']==3
    assert [r['seq'] for r in events(env,group2['id']).json()['items']]==second
    # Earlier 2-part IDs remain readable without a date constraint.
    legacy=encode(decode(group['id'])[:2])
    assert len(events(env,legacy).json()['items'])>=5


def test_group_list_cursor_freezes_snapshot_and_rejects_filter_change(env):
    a=turn(env,'loggroup-list-001'); evidence(env,a,1)
    b=turn(env,'loggroup-list-002'); evidence(env,b,1)
    first=groups(env,role_id='ops',day='2026-09-01',limit=1).json()
    assert first['items'][0]['turn_id']==b and first['next_before']
    evidence(env,a,1)
    second=groups(env,role_id='ops',day='2026-09-01',limit=1,before=first['next_before']).json()
    assert second['items'][0]['turn_id']==a
    assert second['items'][0]['event_count']==1 and second['next_before'] is None
    assert second['snapshot_seq']==first['snapshot_seq']
    assert groups(env,role_id='other',day='2026-09-01',before=first['next_before']).status_code==422
    assert groups(env,role_id='ops',day='2026-09-02',before=first['next_before']).status_code==422


def test_admin_and_cursor_validation(env):
    c,h,*_=env
    assert c.get('/api/v1/logs/groups').status_code in (401,403)
    assert groups(env,limit=0).status_code==422
    assert events(env,'not-valid-base64').status_code==422
    from ai_ops.log_groups import encode
    assert events(env,encode(['turn:none',-1,None])).status_code==422
    assert events(env,encode(['turn:none',10,{}])).status_code==422


def test_standalone_task_keeps_group_identity_and_output_link(env):
    c,h,*_=env
    response=c.post('/api/v1/tasks',headers=h,json={
        'role_id':'ops','asset_id':'host','execution_users':['reader'],
        'run_as':'reader','command':'echo fixture-only','mode':'confirm',
        'idempotency_key':'group-standalone-task-001'})
    assert response.status_code==201,response.text
    task_id=response.json()['id']
    # Legacy standalone history can have no turn; do not fold it into platform day.
    with c.app.state.transaction() as db:
        db.execute('UPDATE tasks SET turn_id=NULL WHERE id=?',(task_id,))
    result=groups(env,role_id='ops').json()
    group=next(r for r in result['items'] if r.get('task_id')==task_id and not r['turn_id'])
    detail=events(env,group['id']).json()['items']
    assert detail and all(r['task_id']==task_id for r in detail)
    assert any(r['entity_id']==task_id for r in detail)
