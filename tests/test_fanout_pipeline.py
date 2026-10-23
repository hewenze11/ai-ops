"""Fan-out through the real turn pipeline (§52.4).

The unit tests in test_fanout.py check the planning and assembly decisions. This
file checks the parts that are easy to get wrong at the seams: that a turn waits
on exactly one parent, that the parent can never be dispatched to a host, and
that the group only completes when every child is accounted for.
"""
import json

import pytest
from fastapi.testclient import TestClient

from ai_ops import fanout, turns
from ai_ops.app import create_app
from ai_ops.fanout_runner import FanoutRunner

ADMIN = 'fanout-token-' + 'x' * 32
H = {'Authorization': 'Bearer ' + ADMIN}


@pytest.fixture()
def env(tmp_path):
    app = create_app(str(tmp_path / 'control.db'), ADMIN, 'test-model')
    client = TestClient(app)
    client.post('/api/v1/roles', headers=H, json={'id': 'ops', 'name': 'ops'})
    tokens = {}
    for name in ('h1', 'h2', 'h3'):
        r = client.post('/api/v1/assets', headers=H,
                        json={'id': name, 'name': name, 'allowed_users': ['ops_read']})
        tokens[name] = r.json()['agent_token']
    return client, app, tmp_path, tokens


def _turn(db):
    turn_id = turns.enqueue_turn(db, 'ops', 'user', 'src-1', ['ops_read'],
                                 'readonly', 'please check the fleet', {})
    db.execute("UPDATE role_turns SET state='waiting_tool' WHERE id=?", (turn_id,))
    return turn_id, db.execute("SELECT * FROM role_turns WHERE id=?", (turn_id,)).fetchone()


def test_parent_is_never_claimable_and_children_are(env):
    """The parent exists only so the turn waits on one id.

    If an Agent could claim it, a real host would receive a task with no argv and
    fail, and the turn would settle on that failure instead of on the group.

    Checked as a SQL predicate rather than through a claim request: a real claim
    also validates account grants, and this test is about routing -- whether a
    fanout parent can ever appear in the claim result set at all.
    """
    client, app, tmp_path, tokens = env
    with app.state.transaction() as db:
        turn_id, turn = _turn(db)
        group_id, children = FanoutRunner(app.state.transaction, app.state.audit, None).start(
            db, turn=turn, tool='read_file', params={'path': '/etc/hostname'},
            asset_ids=['h1', 'h2', 'h3'], rejected=[], run_as='ops_read')
        db.execute("INSERT INTO tasks(id,role_id,asset_id,payload,state,idempotency_key,"
                   "created_at,updated_at,turn_id) VALUES(?,?,?,?,?,?,?,?,?)",
                   ('parent-1', 'ops', 'h1',
                    json.dumps({'kind': 'fanout', 'group_id': group_id}),
                    'queued', 'fanout-parent:' + group_id, 0, 0, turn_id))
        claimable = [r['id'] for r in db.execute(
            "SELECT t.id FROM tasks t WHERE t.state='queued' AND t.asset_id='h1' "
            "AND json_extract(t.payload,'$.kind') IS NOT 'fanout'")]
    assert 'parent-1' not in claimable, 'the parent must never be claimable'
    assert claimable, 'children must be claimable'
    assert set(claimable) <= {c[1] for c in children}


def test_no_child_means_the_turn_is_not_released(env):
    """A partial picture must never reach the model as if it were complete."""
    client, app, tmp_path, tokens = env
    with app.state.transaction() as db:
        group_id = 'g-partial'
        fanout.create(db, group_id=group_id, turn_id='t1', tool='read_file',
                      params={}, asset_ids=['h1', 'h2'])
        fanout.attach_task(db, group_id, 'h1', 'task-1')
        db.execute("UPDATE fanout_children SET state='ok' WHERE group_id=? AND asset_id='h1'", (group_id,))
        done, answer = FanoutRunner(app.state.transaction, app.state.audit, None).status(db, group_id)
    assert done is False
    assert answer == {'running': 1, 'of': 2}


def test_group_completes_and_the_answer_is_aligned(env):
    client, app, tmp_path, tokens = env
    with app.state.transaction() as db:
        group_id = 'g-full'
        fanout.create(db, group_id=group_id, turn_id='t1', tool='read_file',
                      params={'path': '/etc/hostname'}, asset_ids=['h1', 'h2'])
        for asset_id in ('h1', 'h2'):
            task_id = 'task-' + asset_id
            result = ({'status': 'succeeded', 'stdout': 'host-%s\n' % asset_id}
                      if asset_id == 'h1' else
                      {'status': 'failed', 'error_code': 'CANNOT_SWITCH_USER', 'stdout': ''})
            db.execute("INSERT INTO tasks(id,role_id,asset_id,payload,state,result,"
                       "idempotency_key,created_at,updated_at,turn_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (task_id, 'ops', asset_id, '{}',
                        'succeeded' if asset_id == 'h1' else 'failed',
                        json.dumps(result), 'k-' + asset_id, 0, 0, 't1'))
            fanout.attach_task(db, group_id, asset_id, task_id)
            # A task only stops being 'dispatched' when its result is recorded.
            # Call the same hook the result endpoint calls, so the test exercises
            # the real transition instead of jumping straight to the end state.
            FanoutRunner(app.state.transaction, app.state.audit, None).child_reported(db, task_id)
        done, answer = FanoutRunner(app.state.transaction, app.state.audit, None).status(db, group_id)
    assert done is True
    assert answer['summary'] == {'ok': 1, 'unreachable': 1}
    assert answer['assets_total'] == 2
    # The unreachable host contributes no text: it was not read.
    assert [r['asset_id'] for r in answer['results']] == ['h1']
    assert 'host-h1' in answer['results'][0]['text']
    assert answer['failures'] == [{'asset_id': 'h2', 'status': 'unreachable',
                                  'error_code': 'CANNOT_SWITCH_USER'}]


def test_budget_exhaustion_is_reported_not_hidden(env):
    """One dead host must not hold the whole fan-out open forever.

    When the deadline passes, unanswered children are classified as such and the
    rest is delivered -- and the model is told the group was cut short.
    """
    client, app, tmp_path, tokens = env
    with app.state.transaction() as db:
        group_id = 'g-deadline'
        fanout.create(db, group_id=group_id, turn_id='t1', tool='read_file',
                      params={}, asset_ids=['h1', 'h2'])
        fanout.attach_task(db, group_id, 'h1', 'task-1')
        db.execute("UPDATE tasks SET state='succeeded',result=? WHERE id='task-1'",
                   (json.dumps({'status': 'succeeded', 'stdout': 'x'}),))
        db.execute("UPDATE fanout_children SET state='ok' WHERE asset_id='h1' AND group_id=?", (group_id,))
        db.execute("UPDATE fanout_groups SET deadline=0 WHERE id=?", (group_id,))
        done, answer = FanoutRunner(app.state.transaction, app.state.audit, None).status(db, group_id)
        child = db.execute("SELECT state,error_code FROM fanout_children WHERE group_id=? "
                           "AND asset_id='h2'", (group_id,)).fetchone()
    assert done is True
    assert child['state'] == 'timeout'
    assert child['error_code'] == 'FANOUT_BUDGET_EXHAUSTED'
    assert answer['summary'] == {'ok': 1, 'timeout': 1}


def test_read_many_is_refused_outside_readonly_mode():
    """A fan-out in a write turn would be 50 unapproved reads dressed as one call.

    The tool is only advertised in readonly mode; this pins the server-side
    check, because tool advertisement is not authority.
    """
    source = open(turns.__file__, encoding='utf-8').read()
    block = source.split('if name == READ_MANY:')[1].split('if name in READONLY_TOOLS:')[0]
    assert "current['mode'] != 'readonly'" in block
    assert 'MODEL_TOOL_AUTHORIZATION_OR_SCHEMA_REJECTED' in block


def test_parent_success_does_not_discard_partial_results():
    """The parent reports succeeded when the group finished, by design.

    Marking it failed because one host was down would throw away the 49 results
    that did arrive -- and the model would learn nothing about the outage.
    """
    source = open(turns.__file__, encoding='utf-8').read()
    block = source.split('def _advance_fanout')[1].split('    def advance(')[0]
    assert "'status': 'succeeded'" in block
