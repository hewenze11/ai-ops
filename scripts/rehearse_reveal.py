"""End-to-end rehearsal of the operator unlock path, against a real app.

This is not a unit test. It drives the same HTTP endpoints a console and a real
Agent would, in the same order, and then inspects the database to prove where
the plaintext did and did not go. Run it after any change to the reveal,
scrubbing or claim paths:

    python scripts/rehearse_reveal.py

Exit code is the verdict. It uses a throwaway database; nothing is deployed and
no host is touched -- the Agent side is simulated by posting a result directly.
"""
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from ai_ops import sensitive  # noqa: E402
from ai_ops.app import create_app  # noqa: E402

ADMIN = 'rehearsal-token-' + 'x' * 32
ADMIN_HEADERS = {'Authorization': 'Bearer ' + ADMIN}
SECRET = 'sk-rehearsal-abcdefghijklmnopqrstuvwxyz'
PATH = '/etc/app/config.yaml'


def check(label, condition, detail=''):
    status = 'ok  ' if condition else 'FAIL'
    print('  [%s] %s%s' % (status, label, (' -- ' + detail) if detail and not condition else ''))
    return bool(condition)


def _db(db_path):
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    return db


def rehearse(tmp_dir):
    """Drive the whole path. Returns True if every check held."""
    ok = True
    db_path = tmp_dir / 'control.db'
    app = create_app(str(db_path), ADMIN, 'rehearsal-model')
    client = TestClient(app)

    print('\n1. Provision host and role')
    asset = client.post('/api/v1/assets', headers=ADMIN_HEADERS,
                        json={'id': 'web1', 'name': 'web1', 'allowed_users': ['opsread']})
    ok &= check('asset created', asset.status_code in (200, 201), asset.text)
    agent_token = asset.json().get('agent_token', '')
    role = client.post('/api/v1/roles', headers=ADMIN_HEADERS, json={'id': 'ops', 'name': 'ops'})
    ok &= check('role created', role.status_code in (200, 201), role.text)

    print('\n2. Seed the masked read that produced a hit')
    db = _db(db_path)
    payload = {'kind': 'readonly', 'tool': 'read_file', 'program': '/usr/bin/cat',
               'params': {'path': PATH}, 'run_as': 'opsread',
               'execution_users': ['opsread'], 'asset_id': 'web1',
               'mode': 'readonly', 'role_id': 'ops'}
    db.execute('INSERT OR REPLACE INTO tasks(id,role_id,asset_id,payload,state,'
               'idempotency_key,created_at,updated_at,turn_id) VALUES(?,?,?,?,?,?,?,?,?)',
               ('read-1', 'ops', 'web1', json.dumps(payload), 'succeeded', 'read:1', 0, 0, 'turn-1'))
    hit = sensitive.record(db, turn_id='turn-1', task_id='read-1', asset_id='web1',
                           path=PATH, rule_ids=['named_secret'], baseline_version='v')
    db.commit()
    db.close()
    ok &= check('hit recorded', bool(hit))

    print('\n3. Operator unlocks (confirmation must match)')
    wrong = client.post('/api/v1/sensitive-hits/%s/unlock' % hit, headers=ADMIN_HEADERS,
                        json={'confirm_path': '/etc/shadow'})
    ok &= check('wrong path refused 422', wrong.status_code == 422, str(wrong.status_code))
    unlock = client.post('/api/v1/sensitive-hits/%s/unlock' % hit, headers=ADMIN_HEADERS,
                         json={'confirm_path': PATH, 'reason': 'checking token age'})
    ok &= check('unlock accepted', unlock.status_code == 200, unlock.text)
    token = unlock.json().get('reveal_token', '')
    ok &= check('no plaintext in the unlock response', SECRET not in unlock.text)
    again = client.post('/api/v1/sensitive-hits/%s/unlock' % hit, headers=ADMIN_HEADERS, json={})
    ok &= check('second unlock refused 409', again.status_code == 409, str(again.status_code))

    print('\n4. First fetch dispatches, does not reveal')
    first = client.get('/api/v1/sensitive-hits/%s/revealed' % hit, headers=ADMIN_HEADERS,
                       params={'token': token})
    ok &= check('pending answer', first.status_code == 200 and first.json().get('pending'), first.text)
    ok &= check('no bytes yet', first.json().get('revealed') is not True)
    reveal_task = first.json().get('task_id', '')
    ok &= check('a reveal task was queued', bool(reveal_task))
    poll = client.get('/api/v1/sensitive-hits/%s/revealed' % hit, headers=ADMIN_HEADERS,
                      params={'token': token})
    ok &= check('polling still pending, not an error', poll.json().get('pending') is True, poll.text)

    print('\n5. The Agent claims it and reports unmasked bytes')
    db = _db(db_path)
    row = db.execute('SELECT * FROM tasks WHERE id=?', (reveal_task,)).fetchone()
    queued = json.loads(row['payload'])
    ok &= check('reveal task carries kind=reveal', queued.get('kind') == 'reveal')
    ok &= check('reveal task has no parent turn', row['turn_id'] is None)
    ok &= check('reveal task carries the source turn for authority',
                queued.get('source_turn_id') == 'turn-1')
    ok &= check('reveal task keeps the original tool/args',
                queued.get('tool') == 'read_file' and queued.get('args'))
    claim_id = 'c' * 32
    db.execute("UPDATE tasks SET state='claimed',claim_id=? WHERE id=?", (claim_id, reveal_task))
    db.commit()
    db.close()
    report = client.post('/api/v1/agents/web1/tasks/%s/result' % reveal_task,
                         headers={'Authorization': 'Bearer ' + agent_token},
                         json={'claim_id': claim_id, 'status': 'succeeded', 'exit_code': 0,
                               'stdout': 'password: %s\n' % SECRET, 'stderr': ''})
    ok &= check('result accepted', report.status_code == 200, report.text)

    print('\n6. Where did the plaintext go?')
    db = _db(db_path)
    task_row = db.execute('SELECT result FROM tasks WHERE id=?', (reveal_task,)).fetchone()
    stash = db.execute('SELECT * FROM reveal_secret WHERE hit_id=?', (hit,)).fetchone()
    db.close()
    ok &= check('NOT in tasks.result (model-visible)', SECRET not in (task_row['result'] or ''))
    ok &= check('tasks.result says operator-view-only', 'operator view only' in (task_row['result'] or ''))
    ok &= check('IS in reveal_secret', stash is not None and SECRET in (stash['stdout'] or ''))

    print('\n7. The operator reads it once')
    used = client.get('/api/v1/sensitive-hits/%s/revealed' % hit, headers=ADMIN_HEADERS,
                      params={'token': token})
    ok &= check('revealed=true', used.json().get('revealed') is True, used.text)
    ok &= check('value returned', SECRET in (used.json().get('text') or ''))
    spent = client.get('/api/v1/sensitive-hits/%s/revealed' % hit, headers=ADMIN_HEADERS,
                       params={'token': token})
    ok &= check('grant not reusable 409', spent.status_code == 409, str(spent.status_code))
    db = _db(db_path)
    left = db.execute('SELECT COUNT(*) FROM reveal_secret WHERE hit_id=?', (hit,)).fetchone()[0]
    db.close()
    ok &= check('stashed plaintext deleted after use', left == 0, 'rows left: %d' % left)

    print('\n8. Audit trail')
    events = client.get('/api/v1/audit', headers=ADMIN_HEADERS, params={'limit': 200}).json()
    names = [e['event'] for e in events]
    for expected in ('sensitive.unlocked', 'sensitive.reveal_dispatched', 'sensitive.revealed'):
        ok &= check('audited: ' + expected, expected in names)
    ok &= check('audit carries no plaintext', SECRET not in json.dumps(events))
    return ok


def main():
    tmp = tempfile.mkdtemp()
    try:
        ok = rehearse(Path(tmp))
    finally:
        # Windows keeps a handle until the app's sqlite connections drop; a
        # failed cleanup must not turn a green run red.
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n%s' % ('ALL CHECKS PASSED' if ok else 'SOME CHECKS FAILED'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
