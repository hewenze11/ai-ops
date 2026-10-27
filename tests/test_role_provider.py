import concurrent.futures
import json
import os
import sqlite3
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from ai_ops.app import create_app
from ai_ops.model_client import ModelFailure
from ai_ops.role_provider import check_destination, validate_base_url

TOKEN = 'test-admin-00000000000000000000000000'

@pytest.fixture
def env(tmp_path):
    app = create_app(str(tmp_path/'control.db'), TOKEN, default_model='global-model')
    return app, TestClient(app, headers={'Authorization': 'Bearer '+TOKEN}), tmp_path

def independent(model='model-a', key='unit-only-key-a'):
    return {'enabled': True, 'provider_mode': 'independent', 'base_url': 'https://provider.example/v1', 'model': model, 'api_key': key}

def create(c, role, config):
    r = c.post('/api/v1/console/roles', json={'id':role, 'name':role, 'model':config})
    assert r.status_code == 201, r.text

def test_secret_write_once_redaction_permissions_and_retention(env):
    app,c,p=env
    create(c,'a',independent())
    result=c.get('/api/v1/roles/a/model').json()
    assert result['api_key_configured'] and result['max_model_steps'] is None
    assert 'api_key' not in result and 'credential_ref' not in result
    before=list((p/'role-secrets').glob('*.key'))
    assert len(before)==1 and before[0].read_text()=='unit-only-key-a'
    if os.name != 'nt':
        assert before[0].stat().st_mode & 0o777 == 0o600
        assert before[0].parent.stat().st_mode & 0o777 == 0o700
    r=c.put('/api/v1/roles/a/model',json={**independent(key=''), 'max_model_steps':8})
    assert r.status_code==200 and r.json()['api_key_configured']
    assert list((p/'role-secrets').glob('*.key'))==before
    assert c.get('/api/v1/roles/a/model').json()['max_model_steps']==8
    with app.state.transaction() as db:
        for table in ('role_models','audit'):
            assert 'unit-only-key-a' not in str([tuple(r) for r in db.execute('SELECT * FROM '+table)])
    # Blank preserves; explicit clear is allowed with role disabled.
    r=c.put('/api/v1/roles/a/model',json={**independent(key=''), 'enabled':False, 'clear_api_key':True})
    assert r.status_code==200 and not r.json()['api_key_configured']
    assert before[0].exists()  # immutable revisions retained for in-flight snapshot/backup
    assert TestClient(app).put('/api/v1/roles/a/model',json=independent()).status_code==401

@pytest.mark.parametrize('url',['http://provider.example/v1','https://user:pass@provider.example/v1','https://127.0.0.1/v1','https://169.254.169.254/v1','https://[::1]/v1','https://localhost/v1','https://host.local/v1','https://provider.example:8443/v1','https://provider.example/v1?key=secret'])
def test_invalid_destinations(url):
    with pytest.raises(ValueError): validate_base_url(url)

def test_dns_metadata_rejected():
    with patch('ai_ops.role_provider.socket.getaddrinfo', return_value=[(2,1,6,'',('169.254.169.254',443))]):
        with pytest.raises(ModelFailure,match='DESTINATION_REJECTED'): check_destination('https://example.org/v1')

def test_rollback_conflict_and_concurrent_credentials(env):
    app,c,p=env
    create(c,'a',independent())
    # A validation failure after writing a new revision removes it.
    before=set((p/'role-secrets').iterdir())
    bad={**independent(key='not-stored-failed-write'), 'base_url':'https://localhost/v1'}
    assert c.put('/api/v1/roles/a/model',json=bad).status_code==422
    assert set((p/'role-secrets').iterdir())==before
    with app.state.transaction() as db:
        db.execute("CREATE TRIGGER reject_model BEFORE UPDATE ON role_models BEGIN SELECT RAISE(ABORT,'test reject'); END")
    with pytest.raises(sqlite3.IntegrityError): c.put('/api/v1/roles/a/model',json=independent(key='rollback-key'))
    assert set((p/'role-secrets').iterdir())==before
    with app.state.transaction() as db: db.execute('DROP TRIGGER reject_model')
    def save(i):
        return c.put('/api/v1/roles/a/model',json=independent(model='m'+str(i),key='key'+str(i))).status_code
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(save,range(4)))==[200]*4
    with app.state.transaction() as db:
        settings=json.loads(db.execute("SELECT config FROM role_models WHERE role_id='a'").fetchone()[0])
    assert app.state.role_providers.path(settings['credential_ref']).read_text()=='key'+settings['model'][1:]
    assert c.post('/api/v1/console/roles',json={'id':'a','name':'again','model':independent()}).status_code==409
    assert c.post('/api/v1/console/roles',json={'id':'../escape','name':'x','model':independent()}).status_code==422

def test_roles_route_and_archives_use_own_model_key(env):
    app,c,p=env
    create(c,'a',independent())
    create(c,'b',independent('model-b','unit-only-key-b'))
    # Legacy stored roles remain compatible, but new console roles cannot inherit.
    assert c.post('/api/v1/roles', json={'id':'inherited','name':'inherited'}).status_code == 201
    assert c.put('/api/v1/roles/inherited/model', json={'enabled':True,'max_model_steps':6}).status_code == 200
    seen=[]
    def complete(self,body):
        seen.append((body['model'],self.key_file.read_text(),json.dumps(body)))
        return {'message':{'role':'assistant','content':'COMPRESSED: context.\nSUMMARY: short.'}, 'usage':{}, 'model':body['model']}
    class Global:
        def complete(self,body):
            seen.append((body['model'],'global',json.dumps(body)))
            return {'message':{'role':'assistant','content':'done'},'usage':{},'model':body['model']}
    with patch('ai_ops.role_provider.IndependentClient.complete',complete):
        for role in ('a','b','inherited'):
            r=c.post('/api/v1/roles/'+role+'/messages',json={'text':'hello-'+role,'mode':'readonly','idempotency_key':'unit-test-'+role})
            assert r.status_code==202
        for _ in range(3): assert app.state.role_engine.advance(Global())
        assert seen[0][:2]==('model-a','unit-only-key-a')
        assert seen[1][:2]==('model-b','unit-only-key-b')
        assert seen[2][:2]==('global-model','global')
        from ai_ops.memory_worker import archive_once
        assert archive_once(app,Global())
        assert seen[-1][:2]==('model-a','unit-only-key-a')
    assert all('unit-only-key-' not in row[2] for row in seen)
    assert 'hello-b' not in seen[0][2] and 'hello-a' not in seen[1][2]

def test_unlimited_not_large_number_and_existing_migration(env):
    app,c,p=env
    assert c.post('/api/v1/roles', json={'id':'a','name':'a'}).status_code == 201
    assert c.put('/api/v1/roles/a/model', json={'enabled':True}).status_code == 200
    tid=c.post('/api/v1/roles/a/messages',json={'text':'hello','idempotency_key':'unlimited-test'}).json()['turn_id']
    with app.state.transaction() as db: db.execute('UPDATE role_turns SET model_steps=123456789 WHERE id=?',(tid,))
    class Fake:
        def complete(self,body): return {'message':{'role':'assistant','content':'done'},'usage':{},'model':body['model']}
    assert app.state.role_engine.advance(Fake())
    assert c.get('/api/v1/turns/'+tid).json()['state']=='completed'
    c.put('/api/v1/roles/a/model',json={'enabled':True,'max_model_steps':8})
    create_app(str(p/'control.db'),TOKEN)
    assert c.get('/api/v1/roles/a/model').json()['max_model_steps']==8
    for bad in (0,-1,1001):
        assert c.put('/api/v1/roles/a/model',json={'max_model_steps':bad}).status_code==422


def test_backup_restores_role_secret_without_embedding_it(env):
    from ai_ops.backup import create_backup, verify_backup, restore_backup, BackupError
    app,c,p=env
    create(c,'a',independent())
    target=p/'snapshots'/'backup.db'
    meta=create_backup(p/'control.db',target)
    assert meta['role_secret_count']==1 and 'unit-only-key-a' not in json.dumps(meta)
    assert b'unit-only-key-a' not in target.read_bytes()
    verify_backup(target)
    restored=p/'restored'/'control.db'
    restore_backup(target,restored)
    app2=create_app(str(restored),TOKEN)
    with app2.state.transaction() as db:
        settings=json.loads(db.execute("SELECT config FROM role_models WHERE role_id='a'").fetchone()[0])
    assert app2.state.role_providers.path(settings['credential_ref']).read_text()=='unit-only-key-a'
    next(target.parent.glob('*.role-secrets/*.key')).unlink()
    with pytest.raises(BackupError,match='credential backup is missing'): verify_backup(target)


def test_validation_never_echoes_key(env):
    app,c,p=env
    create(c,'a',{'enabled':False})
    for value in ['private-key\nmalformed', {'api_key':'private-key-object'}, 'private-key-'*500]:
        r=c.put('/api/v1/roles/a/model',json={'api_key':value})
        assert r.status_code==422 and 'private-key' not in r.text
    r=c.post('/api/v1/console/roles',json={'name':'bad','model':{'unexpected': 'private-key-extra'}})
    assert r.status_code==422 and 'private-key' not in r.text


def test_transport_redacts_provider_echo_and_pins_dns(env):
    from ai_ops.model_client import OpenAICompatible, NoRedirect
    from ai_ops.role_provider import PublicHTTPSConnection
    app,c,p=env
    key=p/'key'; key.write_text('unit-only-secret-echo')
    client=OpenAICompatible('https://provider.example/v1',key)
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,n): return json.dumps({'choices':[{'message':{'role':'assistant','content':'unit-only-secret-echo'}}]}).encode()
    with patch.object(client.opener,'open',return_value=Response()):
        assert client.complete({'model':'unit'})['message']['content']=='[REDACTED]'
    with pytest.raises(ModelFailure,match='REDIRECT_REJECTED'):
        NoRedirect().redirect_request(None,None,302,'',{},'https://evil.example')
    import unittest.mock as mock
    sock=mock.MagicMock()
    conn=PublicHTTPSConnection('provider.example')
    with patch('ai_ops.role_provider.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))]) as dns, patch('ai_ops.role_provider.socket.socket',return_value=sock), patch.object(conn._context,'wrap_socket',return_value=sock) as wrap:
        conn.connect()
        assert dns.call_count==1
        sock.connect.assert_called_once_with(('8.8.8.8',443))
        wrap.assert_called_once_with(sock,server_hostname='provider.example')
