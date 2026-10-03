import sys, os, sqlite3, json
sys.path.insert(0,'.')
sys.path.insert(0,'tests')
os.environ.setdefault('X','1')
import tempfile
from ai_ops import connector_ssh
from ai_ops.app import create_app
from fastapi.testclient import TestClient
d = tempfile.mkdtemp()
app = create_app(os.path.join(d,'t.db'), 'test-admin-credential-not-real-1234567890')
c = TestClient(app); h={'Authorization':'Bearer test-admin-credential-not-real-1234567890'}
c.post('/api/v1/roles', headers=h, json={'id':'ops','name':'Ops'})
ref = os.path.join(d,'k'); open(ref,'w').write('k')
r = c.post('/api/v1/assets', headers=h, json={'id':'s','name':'s','allowed_users':['reader','operator'],'notes':'','connection_type':'ssh','ssh_host':'h','ssh_user':'root','ssh_auth_kind':'key','ssh_secret_ref':ref})
print('asset', r.status_code)
class Ch:
    def __init__(s): s.o=bytearray(b'hello\n'); s.e=bytearray(); s.closed=False
    def settimeout(s,_): pass
    def exec_command(s,_): pass
    def recv_ready(s): return bool(s.o)
    def recv(s,n): d=bytes(s.o[:n]); del s.o[:n]; return d
    def recv_stderr_ready(s): return bool(s.e)
    def recv_stderr(s,n): return b''
    def exit_status_ready(s): return not s.o and not s.e
    def recv_exit_status(s): return 0
    def close(s): s.closed=True
class CL:
    def get_transport(s): return type('T',(),{'open_session':lambda self,timeout=None: ch})()
    def close(s): pass
ch=Ch()
connector_ssh._connect = lambda info: CL()
task = c.post('/api/v1/tasks', headers=h, json={'role_id':'ops','asset_id':'s','execution_users':['reader'],'run_as':'reader','command':'id','idempotency_key':'k'*10}).json()
path=os.path.join(d,'t.db')
import contextlib
@contextlib.contextmanager
def tx():
    db=sqlite3.connect(path); db.row_factory=sqlite3.Row; db.execute('BEGIN IMMEDIATE')
    try: yield db; db.commit()
    finally: db.close()
def aud(db,e,i,a,dt): db.execute('INSERT INTO audit(event,entity_id,actor,details,created_at) VALUES(?,?,?,?,0)',(e,i,a,json.dumps(dt)))
ex = connector_ssh.ConnectorExecutor(tx, aud)
cl = ex.claim('s')
print('claimed', cl is not None)
res = ex.run('s', cl, None)
print('RESULT', res)
