import json
import sqlite3
import pytest
from fastapi.testclient import TestClient
from ai_ops.app import create_app

TOKEN = 'asset-memory-test-admin-123456789012345'

@pytest.fixture
def env(tmp_path):
    path = tmp_path/'memory.db'
    app = create_app(str(path), TOKEN, 'fake-model')
    c = TestClient(app, headers={'Authorization': 'Bearer '+TOKEN})
    for asset_id in ('a', 'b'):
        assert c.post('/api/v1/assets', json={'id': asset_id, 'name': asset_id, 'allowed_users': ['reader']}).status_code == 201
    assert c.post('/api/v1/roles', json={'id': 'role', 'name': 'role'}).status_code == 201
    return c, app, path

def test_unique_isolated_revision_limits_auth_and_legacy(env):
    c, app, path = env
    assert c.get('/api/v1/assets/a/memory').json()['revision'] == 0
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM asset_memory').fetchone()[0] == 0
    def put(asset, content, rev=0):
        return c.put('/api/v1/assets/'+asset+'/memory', json={'content': content, 'expected_revision': rev})
    assert put('a', 'A专属').status_code == 200
    assert put('b', 'B专属').status_code == 200
    assert put('a', '冲突').status_code == 409
    assert put('a', '', 1).json()['revision'] == 2
    assert c.get('/api/v1/assets/a/memory').json()['content'] == ''
    assert c.get('/api/v1/assets/b/memory').json()['content'] == 'B专属'
    assert put('missing', 'text').status_code == 404
    assert c.get('/api/v1/assets/missing/memory').status_code == 404
    assert put('a', 'x'*20001, 2).status_code == 422
    assert c.get('/api/v1/asset-memories?limit=101').status_code == 422
    assert c.get('/api/v1/asset-memories?offset=-1').status_code == 422
    page = c.get('/api/v1/asset-memories?limit=1').json()
    assert len(page['items']) == 1 and page['next_offset'] == 1
    anonymous = TestClient(app)
    assert anonymous.get('/api/v1/asset-memories').status_code == 401
    assert anonymous.get('/api/v1/assets/a/memory').status_code == 401
    assert anonymous.put('/api/v1/assets/a/memory', json={'content':'bad','expected_revision':2}).status_code == 401
    legacy = {'day': '2026-10-01', 'full_text': '跨资产旧角色记忆'}
    assert c.put('/api/v1/roles/role/memory/2026-10-01', json=legacy).status_code == 200
    create_app(str(path), TOKEN)
    assert c.get('/api/v1/roles/role/memory/2026-10-01').json()['full_text'] == legacy['full_text']
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM asset_memory').fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO asset_memory VALUES('a','duplicate',1,0,'admin')")
    assert c.post('/api/v1/assets/a/retire', json={'note':'test'}).status_code == 200
    assert c.get('/api/v1/assets/a/memory').json()['revision'] == 2
    assert c.post('/api/v1/assets/a/purge', json={'confirm_asset_id':'a'}).status_code == 200
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM asset_memory WHERE asset_id='a'").fetchone()[0] == 0

@pytest.mark.parametrize('asset_id', ['a','b','missing'])
def test_fake_model_exact_asset_tool_context(env, asset_id):
    c, app, path = env
    for asset, text in [('a','ONLY_A_记录'),('b','ONLY_B_记录')]:
        c.put('/api/v1/assets/'+asset+'/memory', json={'content':text,'expected_revision':0})
    c.put('/api/v1/roles/role/model', json={'enabled':True})
    turn = c.post('/api/v1/roles/role/messages', json={'text':'查资产记忆','mode':'readonly','execution_users':[],'idempotency_key':'memory-test-001'}).json()['turn_id']
    class FakeModel:
        def __init__(self): self.requests=[]
        def complete(self, body):
            self.requests.append(body)
            if len(self.requests) == 1:
                assert 'read_asset_memory' in [t['function']['name'] for t in body['tools']]
                return {'message': {'role':'assistant','content':None,'tool_calls':[{'id':'memory-call','type':'function','function':{'name':'read_asset_memory','arguments':json.dumps({'asset_id':asset_id})}}]}}
            result = json.loads(next(m['content'] for m in body['messages'] if m['role']=='tool' and m['tool_call_id']=='memory-call'))
            if asset_id == 'missing': assert result == {'error':'ASSET_NOT_FOUND_OR_INVALID_ID'}
            else:
                assert result['asset_id'] == asset_id
                assert result['content'] == 'ONLY_'+asset_id.upper()+'_记录'
                other = 'ONLY_B_记录' if asset_id=='a' else 'ONLY_A_记录'
                assert other not in json.dumps(body, ensure_ascii=False)
            return {'message':{'role':'assistant','content':'已读取'}}
    model = FakeModel()
    assert app.state.role_engine.advance(model)
    assert app.state.role_engine.advance(model)
    assert c.get('/api/v1/turns/'+turn).json()['state'] == 'completed'
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
        assert db.execute('SELECT SUM(revision) FROM asset_memory').fetchone()[0] == 2
