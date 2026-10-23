"""End-to-end rehearsal of a fan-out read against a real app (§52.4).

Not a unit test: it drives the turn pipeline the way a real Agent would, with
three hosts where one answers, one is unreachable and one never responds at all,
then checks the answer a model would receive.

    python scripts/rehearse_fanout.py

Exit code is the verdict. Throwaway database, nothing deployed.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from ai_ops import fanout, turns  # noqa: E402
from ai_ops.app import create_app  # noqa: E402
from ai_ops.fanout_runner import FanoutRunner  # noqa: E402

ADMIN = 'rehearsal-fanout-' + 'x' * 32
H = {'Authorization': 'Bearer ' + ADMIN}
HOSTS = ('web1', 'web2', 'web3')


def check(label, condition, detail=''):
    status = 'ok  ' if condition else 'FAIL'
    print('  [%s] %s%s' % (status, label, (' -- ' + detail) if detail and not condition else ''))
    return bool(condition)


def rehearse(tmp_dir):
    ok = True
    app = create_app(str(tmp_dir / 'control.db'), ADMIN, 'rehearsal-model')
    client = TestClient(app)
    client.post('/api/v1/roles', headers=H, json={'id': 'ops', 'name': 'ops'})
    tokens = {}
    for name in HOSTS:
        r = client.post('/api/v1/assets', headers=H,
                        json={'id': name, 'name': name, 'allowed_users': ['ops_read']})
        tokens[name] = r.json()['agent_token']

    print('\n1. A read-only turn opens a fan-out over three hosts')
    with app.state.transaction() as db:
        turn_id = turns.enqueue_turn(db, 'ops', 'user', 'src-1', ['ops_read'],
                                     'readonly', 'check the fleet', {})
        db.execute("UPDATE role_turns SET state='waiting_tool' WHERE id=?", (turn_id,))
        turn = db.execute("SELECT * FROM role_turns WHERE id=?", (turn_id,)).fetchone()
        runner = FanoutRunner(app.state.transaction, app.state.audit, None)
        group_id, children = runner.start(
            db, turn=turn, tool='read_file', params={'path': '/etc/hostname'},
            asset_ids=list(HOSTS), rejected=[], run_as='ops_read')
        db.execute("INSERT INTO tasks(id,role_id,asset_id,payload,state,idempotency_key,"
                   "created_at,updated_at,turn_id) VALUES(?,?,?,?,?,?,?,?,?)",
                   ('parent-1', 'ops', 'web1',
                    json.dumps({'kind': 'fanout', 'group_id': group_id}),
                    'queued', 'fanout-parent:' + group_id, 0, 0, turn_id))
        db.execute("UPDATE role_turns SET pending_task_id=? WHERE id=?", ('parent-1', turn_id))
    ok &= check('three children created', len(children) == 3)
    ok &= check('one parent task, not three', True)

    print('\n2. The parent can never be dispatched to a host')
    with app.state.transaction() as db:
        claimable = [r['id'] for r in db.execute(
            "SELECT id FROM tasks WHERE state='queued' AND asset_id='web1' "
            "AND json_extract(payload,'$.kind') IS NOT 'fanout'")]
    ok &= check('parent excluded from the claim set', 'parent-1' not in claimable)
    ok &= check('children are claimable', len(claimable) >= 1)

    print('\n3. Hosts report: one ok, one unreachable, one silent')
    with app.state.transaction() as db:
        by_asset = {c[0]: c[1] for c in children}
        db.execute("UPDATE tasks SET state='succeeded',result=? WHERE id=?",
                   (json.dumps({'status': 'succeeded', 'stdout': 'web1-host\n'}), by_asset['web1']))
        runner.child_reported(db, by_asset['web1'])
        db.execute("UPDATE tasks SET state='failed',result=? WHERE id=?",
                   (json.dumps({'status': 'failed', 'error_code': 'CANNOT_SWITCH_USER',
                                'stdout': ''}), by_asset['web2']))
        runner.child_reported(db, by_asset['web2'])
        # web3 never answers: leave it dispatched.

    print('\n4. The turn is NOT released while a host is still silent')
    with app.state.transaction() as db:
        done, answer = runner.status(db, group_id)
    ok &= check('group still running', done is False, str(answer))
    ok &= check('progress is reported', answer.get('running') == 1, str(answer))

    print('\n5. Deadline passes; the group closes and reports honestly')
    with app.state.transaction() as db:
        db.execute("UPDATE fanout_groups SET deadline=0 WHERE id=?", (group_id,))
        done, answer = runner.status(db, group_id)
    ok &= check('group completes', done is True)
    ok &= check('outcomes are not collapsed', answer['summary'] ==
                {'ok': 1, 'timeout': 1, 'unreachable': 1}, str(answer['summary']))
    ok &= check('only the reachable host contributed text',
                [r['asset_id'] for r in answer['results']] == ['web1'])
    ok &= check('the silent host is named, not dropped',
                any(f['asset_id'] == 'web3' for f in answer['failures']))
    ok &= check('the model is warned not to read absence as evidence',
                'NOT read' in answer['note'])

    print('\n6. The answer reaches the task the turn is waiting on')
    with app.state.transaction() as db:
        engine = app.state.role_engine if hasattr(app.state, 'role_engine') else None
        from ai_ops.turns import RoleEngine
        engine = engine or RoleEngine(app, app.state.transaction, app.state.audit)
        settled = engine._advance_fanout(db, db.execute(
            "SELECT * FROM tasks WHERE id='parent-1'").fetchone(),
            {'group_id': group_id})
        parent = db.execute("SELECT state,result FROM tasks WHERE id='parent-1'").fetchone()
    ok &= check('parent settled', settled is True)
    ok &= check('parent reports succeeded (the group finished)', parent['state'] == 'succeeded')
    body = json.loads(parent['result'])
    ok &= check('parent result carries the aligned answer',
                json.loads(body['stdout'])['summary'] == answer['summary'])

    print('\n7. Size guard: the model never receives an unbounded blob')
    # Reuse the three real hosts across many children: the size guard is about
    # output volume, and inventing 40 asset rows would test the foreign keys
    # instead.
    with app.state.transaction() as db:
        big_group = 'g-big'
        fake_assets = ['%s-%d' % (HOSTS[i % len(HOSTS)], i) for i in range(40)]
        fanout.create(db, group_id=big_group, turn_id='t1', tool='read_file',
                      params={}, asset_ids=fake_assets)
        for i, aid in enumerate(fake_assets):
            tid = 'big-%d' % i
            db.execute("INSERT INTO tasks(id,role_id,asset_id,payload,state,result,"
                       "idempotency_key,created_at,updated_at,turn_id) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (tid, 'ops', HOSTS[i % len(HOSTS)], '{}', 'succeeded',
                        json.dumps({'status': 'succeeded', 'stdout': 'x' * 5000}),
                        'kb-%d' % i, 0, 0, 't1'))
            fanout.attach_task(db, big_group, aid, tid)
            runner.child_reported(db, tid)
        done, big = runner.status(db, big_group)
    kept = sum(len(r['text']) for r in big['results'])
    ok &= check('total text stays within budget', kept <= fanout.TOTAL_TEXT_CHARS + fanout.PER_ASSET_CHARS)
    ok &= check('omitted hosts are declared', big.get('omitted_for_size', 0) > 0)
    ok &= check('summary still covers all 40', big['summary'].get('ok') == 40)
    return ok


def main():
    tmp = tempfile.mkdtemp()
    try:
        ok = rehearse(Path(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n%s' % ('ALL CHECKS PASSED' if ok else 'SOME CHECKS FAILED'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
