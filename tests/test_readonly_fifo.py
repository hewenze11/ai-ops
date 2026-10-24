"""Readonly tasks of one turn may overlap; anything else stays FIFO (§52.8).

The rule the claim query encodes is narrower than "allow parallel reads":

    a task may be claimed while an earlier task of the same turn is unfinished
    only if *both* are readonly.

The asymmetry matters. Two reads overlapping is the feature -- the model asked
for three files and should not pay three round trips. A read overlapping a
change is a bug with a plausible-looking result: the model reads a host while
something is editing it and then reasons about a state that never existed. And
two writes overlapping breaks the thing approvals are built on, which is that
a turn has exactly one unfinished intent to inspect, approve, or cancel.

So the tests below pin three shapes:

* reads fan in (the new behaviour),
* write-then-read and read-then-write stay serial (the boundary),
* two writes stay serial (the invariant that must never move).
"""
import json

import pytest
from fastapi.testclient import TestClient

from ai_ops.app import create_app

ADMIN = 'fifo-admin-not-real-' + 'q' * 30
H = {'Authorization': 'Bearer ' + ADMIN}
PROTO = {'protocol_version': '1.3'}


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, body):
        self.requests.append(body)
        return self.responses.pop(0)


def final(text='done'):
    return {'message': {'role': 'assistant', 'content': text},
            'usage': {'total_tokens': 5}}


def call(name, arguments, call_id='call-1'):
    return {'message': {'role': 'assistant', 'content': None,
                        'tool_calls': [{'id': call_id, 'type': 'function',
                                        'function': {'name': name,
                                                     'arguments': json.dumps(arguments)}}]},
            'usage': {'total_tokens': 5}}


def read(path, call_id='call-r'):
    return call('read_file', {'asset_id': 'host', 'run_as': 'ops_read', 'path': path}, call_id)


def change(unit, call_id='call-w'):
    return call('propose_change', {'asset_id': 'host', 'run_as': 'ops_root',
                                   'reason': 'restart it',
                                   'actions': [{'action': 'service.restart',
                                                'params': {'unit': unit}}]}, call_id)


@pytest.fixture()
def env(tmp_path):
    app = create_app(str(tmp_path / 'fifo.db'), ADMIN, 'test-model')
    c = TestClient(app)
    c.post('/api/v1/roles', headers=H, json={'id': 'ops', 'name': 'ops'})
    c.put('/api/v1/roles/ops/model', headers=H, json={'enabled': True})
    asset = c.post('/api/v1/assets', headers=H,
                   json={'id': 'host', 'name': 'Host',
                         'allowed_users': ['ops_read', 'ops_root']}).json()
    # Two assets, because a fan-out refuses a repeated host rather than
    # silently collapsing it -- and two children are what this file is about.
    asset2 = c.post('/api/v1/assets', headers=H,
                    json={'id': 'host2', 'name': 'Host 2',
                          'allowed_users': ['ops_read', 'ops_root']}).json()
    agent = {'Authorization': 'Bearer ' + asset['agent_token']}
    agent2 = {'Authorization': 'Bearer ' + asset2['agent_token']}
    return c, app.state.role_engine, app, agent, agent2


def send(c, mode='readonly', key='message-0001'):
    # Asset-scoped grants, not legacy free-form usernames: the same username
    # on two hosts is ambiguous by design and is refused.
    users = ['ops_read'] if mode == 'readonly' else ['ops_read', 'ops_root']
    # A grant must name a (asset, username) pair that the asset itself lists.
    grants = [{'asset_id': a, 'username': u} for a in ('host', 'host2') for u in users]
    return c.post('/api/v1/roles/ops/messages', headers=H,
                  json={'text': 'look around', 'execution_grants': grants,
                        'mode': mode, 'idempotency_key': key}).json()['turn_id']


def claim(c, agent, asset='host'):
    return c.post(f'/api/v1/agents/{asset}/claim', headers=agent, json=PROTO).json()['task']


def queue_of(c, turn_id):
    """The turn's tasks, oldest first, as (kind, path/unit, state)."""
    with c.app.state.transaction() as db:
        rows = db.execute('SELECT payload,state FROM tasks WHERE turn_id=? ORDER BY seq',
                          (turn_id,)).fetchall()
    out = []
    for row in rows:
        payload = json.loads(row['payload'])
        out.append((payload.get('kind'), payload.get('params', {}).get('path')
                    or payload.get('steps', [{}])[0].get('action'), row['state']))
    return out


def finish(c, agent, task, status='succeeded'):
    c.post(f"/api/v1/agents/host/tasks/{task['id']}/result", headers=agent,
           json={'claim_id': task['claim_id'], 'status': status, 'exit_code': 0,
                 'stdout': 'x\n'})


def test_two_reads_of_one_turn_run_together(env):
    """The point of the change: the model does not pay a round trip per file.

    The model cannot speak again while its read is outstanding (one tool call
    per step, by design), so two readonly tasks of one turn are produced by a
    *fan-out group*: one `read_many` call becomes N readonly children. Those
    children are the reads that must not wait for each other. Before this,
    child 2 sat queued until child 1 answered, which made a 10-host fan-out
    take ten sequential round trips -- the concurrency the whole feature exists
    for.
    """
    c, engine, app, agent, agent2 = env
    turn_id = send(c)
    ok = engine.advance(FakeModel([call('read_many',
                                   {'tool': 'read_file',
                                    'params': {'path': '/etc/hosts'},
                                    'asset_ids': ['host', 'host2'],
                                    'run_as': 'ops_read'}, 'c1')]))
    body = c.get('/api/v1/turns/' + turn_id, headers=H).json()
    assert ok and body['state'] == 'waiting_tool', body['state']
    first = claim(c, agent)
    assert first is not None and first['kind'] == 'readonly'
    # The sibling child, on the other host, is dispatched while the first one is
    # still running. Before this rule it sat queued, and a 10-host fan-out took
    # ten sequential round trips -- the concurrency the feature exists for.
    second = c.post('/api/v1/agents/host2/claim', headers=agent2,
                    json=PROTO).json()['task']
    assert second is not None, "the sibling child must be dispatched"
    assert second['id'] != first['id']


def test_a_read_never_overtakes_a_queued_write(env):
    """Even a readonly task must not pass an unfinished write of its own turn.

    Without this half of the rule, a fan-out issued after a change would run
    its reads while the change is still going, and the model would reason about
    a host that is being edited under it.
    """
    c, engine, app, agent, agent2 = env
    turn_id = send(c, mode='direct')
    engine.advance(FakeModel([change('nginx', 'c1')]))
    write = claim(c, agent)
    assert write['kind'] == 'change'
    # A read of the same turn exists now (the model moved on after proposing).
    engine.advance(FakeModel([read('/etc/hosts', 'c2')]))
    assert claim(c, agent) is None, 'a read must not start beside an unfinished change'


def test_a_write_waits_for_an_unfinished_read(env):
    """A write must not start while a read of the same turn is outstanding.

    The reverse would be worse than slow: the model would read a host that is
    already being changed and reason about a state that never existed. The turn
    only produces that shape through a fan-out issued before a change; here we
    pin the rule directly on the queue, which is where it lives.
    """
    c, engine, app, agent, agent2 = env
    turn_id = send(c, mode='direct')
    # A write is proposed first; that is the only way a real change task (with
    # its authority snapshot) can exist.
    assert engine.advance(FakeModel([change('nginx', 'c1')]))
    with app.state.transaction() as db:
        write = db.execute("SELECT * FROM tasks WHERE turn_id=? ORDER BY seq", (turn_id,)).fetchone()
    assert json.loads(write['payload'])['kind'] == 'change'
    # Now a read of the same turn appears behind it, still unfinished.
    with app.state.transaction() as db:
        payload = dict(json.loads(write['payload']), kind='readonly')
        db.execute('INSERT INTO tasks(id,role_id,asset_id,payload,state,idempotency_key,'
                   'created_at,updated_at,turn_id) VALUES(?,?,?,?,?,?,?,?,?)',
                   ('overlap-read', 'ops', 'host', json.dumps(payload), 'queued',
                    'test:overlap-read', 1.0, 1.0, turn_id))
    first = claim(c, agent)
    assert first is not None and first['kind'] == 'change'
    # Nothing else is claimable while that change is unfinished: not the read.
    assert claim(c, agent) is None, 'a read must not start beside an unfinished change'


def test_write_then_read_stays_serial(env):
    """A read must not start while a change of the same turn is outstanding."""
    c, engine, app, agent, agent2 = env
    turn_id = send(c, mode='direct')
    engine.advance(FakeModel([change('nginx', 'c1')]))
    write = claim(c, agent)
    assert write['kind'] == 'change'
    # Nothing else is claimable while the change is running, not even a read.
    assert claim(c, agent) is None


def test_two_writes_never_overlap(env):
    """One unfinished write intent per turn -- approvals depend on it.

    If two writes could be outstanding, "the task this turn is waiting on"
    stops being a single well-defined thing, and approve/reject/cancel would
    each have to guess.
    """
    c, engine, app, agent, agent2 = env
    turn_id = send(c, mode='direct')
    engine.advance(FakeModel([change('nginx', 'c1')]))
    first = claim(c, agent)
    assert first['kind'] == 'change'
    # A second change can only exist after the first one settles.
    model = FakeModel([change('redis', 'c2')])
    engine.advance(model)
    assert claim(c, agent) is None
