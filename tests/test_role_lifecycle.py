"""Role archive preserves evidence and rejects active references; no external I/O."""
import json
import pytest
from test_turns import env


def message(env, role='ops'):
    c,h,*_=env
    r=c.post('/api/v1/roles/'+role+'/messages',headers=h,json={
        'text':'archive fixture','mode':'readonly','execution_grants':[],
        'idempotency_key':'lifecycle-fixture-'+role})
    assert r.status_code==202,r.text
    return r.json()['turn_id']


@pytest.mark.parametrize('blocker',['active','unknown','channel','custom'])
def test_role_archive_blockers(env,blocker):
    c,h,engine,*_=env
    if blocker in ('active','unknown'):
        tid=message(env)
        if blocker=='unknown':
            with engine.transaction() as db:db.execute("UPDATE role_turns SET state='blocked_unknown' WHERE id=?",(tid,))
    elif blocker=='channel':
        with engine.transaction() as db:
            db.execute('INSERT INTO channel_identities(channel,user_id,role_id,mode,execution_users,created_at) VALUES(?,?,?,?,?,?)',('feishu','fixture-user','ops','readonly','[]',1))
    else:
        r=c.post('/api/v1/custom-tasks',headers=h,json={'id':'archive-linked','name':'Linked','role_id':'ops','kind':'trigger','prompt':'fixture','mode':'readonly','execution_grants':[]})
        assert r.status_code==201,r.text
    assert c.delete('/api/v1/roles/ops',headers=h).status_code==409
    with engine.transaction() as db:assert db.execute('SELECT count(*) FROM retired_roles').fetchone()[0]==0


def test_archive_retains_history_memory_provider_and_is_idempotent(env):
    c,h,engine,*_=env
    tid=message(env)
    with engine.transaction() as db:
        db.execute("UPDATE role_turns SET state='completed' WHERE id=?",(tid,))
        db.execute('INSERT INTO role_memory VALUES(?,?,?,?,?,?,?,?,?)',('ops','2026-10-09','FULL','COMPRESSED','SUMMARY','fixture',1,None,1))
        before_model=db.execute("SELECT config FROM role_models WHERE role_id='ops'").fetchone()[0]
        before_audit=[tuple(r) for r in db.execute('SELECT * FROM audit ORDER BY seq')]
    first=c.delete('/api/v1/roles/ops',headers=h)
    assert first.status_code==200 and first.json()['history_retained']
    assert c.delete('/api/v1/roles/ops',headers=h).json()==first.json()
    assert 'ops' not in [r['id'] for r in c.get('/api/v1/roles',headers=h).json()]
    assert c.get('/api/v1/turns/'+tid,headers=h).status_code==200
    with engine.transaction() as db:
        assert db.execute('SELECT id FROM role_turns WHERE id=?',(tid,)).fetchone()
        assert db.execute("SELECT full_text FROM role_memory WHERE role_id='ops'").fetchone()[0]=='FULL'
        assert db.execute("SELECT config FROM role_models WHERE role_id='ops'").fetchone()[0]==before_model
        audits=[tuple(r) for r in db.execute('SELECT * FROM audit ORDER BY seq')]
        assert audits[:len(before_audit)]==before_audit
        assert db.execute("SELECT COUNT(*) FROM audit WHERE entity_id='ops' AND event='role.archived'").fetchone()[0]==1
    r=c.post('/api/v1/roles/ops/messages',headers=h,json={'text':'must not queue','idempotency_key':'after-archive-new-message'})
    assert r.status_code==409
    assert c.get('/api/v1/roles/other/model',headers=h).status_code==200
