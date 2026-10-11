"""Account-note dependents participate in guarded asset purge transactions."""
import sqlite3
import pytest
from test_control import env, submit


def add_notes(c, h, aid):
    result = c.put(f'/api/v1/assets/{aid}/notes', headers=h, json={
        'name': f'{aid} edited', 'allowed_users': ['reader','operator'],
        'account_notes': {'reader':'只读排查','operator':'维护'}, 'notes':'多行\n资产说明'})
    assert result.status_code == 200, result.text


def test_purge_console_asset_with_notes_and_rotated_token(env):
    c, h, _, path = env
    r = c.post('/api/v1/console/assets', headers=h, json={'name':'New fixture', 'allowed_users':['reader'], 'account_notes':{'reader':'用途'}, 'connection_type':'agent'})
    assert r.status_code == 201, r.text
    aid = r.json()['asset_id']
    assert c.post(f'/api/v1/assets/{aid}/rotate-token', headers=h, json={'grace_seconds':0}).status_code == 200
    add_notes(c,h,'b')
    r = c.post(f'/api/v1/assets/{aid}/purge', headers=h, json={'confirm_asset_id':aid})
    assert r.status_code == 200, r.text
    assert r.json()['counts']['account_notes'] == 1
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM assets WHERE id=?',(aid,)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM asset_account_notes WHERE asset_id=?',(aid,)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM asset_account_notes WHERE asset_id='b'").fetchone()[0] == 2
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert any(a['event']=='asset.purged' and a['entity_id']==aid for a in c.get('/api/v1/audit?limit=100',headers=h).json())


@pytest.mark.parametrize('state',['claimed','unknown'])
def test_unresolved_purge_keeps_accounts_and_tasks(env,state):
    c,h,_,path=env
    add_notes(c,h,'a')
    task=submit(env).json()
    with sqlite3.connect(path) as db:
        db.execute('UPDATE tasks SET state=? WHERE id=?',(state,task['id']))
    r=c.post('/api/v1/assets/a/purge',headers=h,json={'confirm_asset_id':'a'})
    assert r.status_code == 409
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM asset_account_notes WHERE asset_id='a'").fetchone()[0] == 2
        assert db.execute('SELECT state FROM tasks WHERE id=?',(task['id'],)).fetchone()[0] == state
        assert db.execute("SELECT count(*) FROM assets WHERE id='a'").fetchone()[0] == 1
