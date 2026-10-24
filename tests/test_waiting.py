"""Wait handles: run a long job without blocking the turn (§52.5).

The three ways a wait can end are the whole point of this file. Two of them
(abandoned, expired) are *not* failures, and the test names say so, because the
bug this design exists to prevent is the model reading "I stopped waiting" as
"the build broke".
"""
import json

import pytest
from fastapi.testclient import TestClient

from ai_ops import waiting
from ai_ops.app import create_app
from ai_ops.turns import PROPOSE_CHANGE, START_TASK

ADMIN = 'wait-test-admin-not-real-' + 'y' * 30
H = {'Authorization': 'Bearer ' + ADMIN}


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, body):
        self.requests.append(body)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def final(text='done'):
    return {'message': {'role': 'assistant', 'content': text}, 'usage': {'total_tokens': 5}}


def call(name, arguments, call_id='call-1'):
    return {'message': {'role': 'assistant', 'content': None,
                        'tool_calls': [{'id': call_id, 'type': 'function',
                                        'function': {'name': name,
                                                     'arguments': json.dumps(arguments)}}]},
            'usage': {'total_tokens': 5}}


def start_args(extra=None):
    args = {'asset_id': 'host', 'run_as': 'operator', 'reason': 'rebuild',
            'actions': [{'action': 'service.restart', 'params': {'unit': 'nginx'}}]}
    return {**args, **(extra or {})}


@pytest.fixture()
def env(tmp_path):
    app = create_app(str(tmp_path / 'wait.db'), ADMIN, 'test-model')
    c = TestClient(app)
    c.post('/api/v1/roles', headers=H, json={'id': 'ops', 'name': 'ops'})
    c.put('/api/v1/roles/ops/model', headers=H, json={'enabled': True})
    asset = c.post('/api/v1/assets', headers=H,
                   json={'id': 'host', 'name': 'Host',
                         'allowed_users': ['reader', 'operator']}).json()
    agent = {'Authorization': 'Bearer ' + asset['agent_token']}
    return c, app.state.role_engine, app, agent


def send(c, mode='direct'):
    return c.post('/api/v1/roles/ops/messages', headers=H,
                  json={'text': 'rebuild the service', 'execution_users': ['operator'],
                        'mode': mode, 'idempotency_key': 'message-0001'}).json()['turn_id']


def turn(c, turn_id):
    return c.get('/api/v1/turns/' + turn_id, headers=H).json()


def test_start_task_parks_on_a_handle_and_does_not_block(env):
    """The turn answers immediately with a handle; the job is not its problem."""
    c, engine, app, agent = env
    turn_id = send(c)
    model = FakeModel([call(START_TASK, start_args())])
    assert engine.advance(model)
    body = turn(c, turn_id)
    assert body['state'] == 'waiting_task'
    assert body['wait']['state'] == 'waiting'
    # The handle reached the model as tool output, so it can come back to it.
    handle = body['wait']['handle']
    tool_output = json.loads(body['messages'][-1]['content'])
    assert tool_output['handle'] == handle and tool_output['started'] is True
    # And the job is a normal change task, queued under the same authority rules.
    task = c.post('/api/v1/agents/host/claim', headers=agent,
                  json={'protocol_version': '1.3'}).json()['task']
    assert task['kind'] == 'change'


def test_finished_job_wakes_the_turn_with_its_result(env):
    c, engine, app, agent = env
    turn_id = send(c)
    model = FakeModel([call(START_TASK, start_args()), final('build finished')])
    engine.advance(model)
    task = c.post('/api/v1/agents/host/claim', headers=agent,
                  json={'protocol_version': '1.3'}).json()['task']
    c.post(f"/api/v1/agents/host/tasks/{task['id']}/result", headers=agent,
           json={'claim_id': task['claim_id'], 'status': 'succeeded', 'exit_code': 0,
                 'stdout': 'restarted\n'})
    assert engine.advance(model)
    body = turn(c, turn_id)
    assert body['state'] == 'completed'
    assert body['final_text'] == 'build finished'
    seen = json.loads(body['messages'][-2]['content'])
    assert seen['status'] == 'succeeded' and seen['waited_out'] == 'finished'


def test_stop_waiting_resumes_the_turn_and_leaves_the_job_running(env):
    """The central distinction: the wait ended, the build did not.

    If this surfaced as an error, the model would retry a build that is still
    running -- on a real host, that is two builds.
    """
    c, engine, app, agent = env
    turn_id = send(c)
    model = FakeModel([call(START_TASK, start_args()), final('moved on')])
    engine.advance(model)
    task = c.post('/api/v1/agents/host/claim', headers=agent,
                  json={'protocol_version': '1.3'}).json()['task']
    r = c.post(f'/api/v1/turns/{turn_id}/stop-waiting', headers=H)
    assert r.status_code == 200 and r.json()['job_keeps_running'] is True
    body = turn(c, turn_id)
    assert body['state'] == 'ready'
    seen = json.loads(body['messages'][-1]['content'])
    assert seen['still_running'] is True
    assert seen['task_state'] == 'claimed'
    assert 'Do not start it again' in seen['note']
    # The job itself is untouched: still claimed, still the agent's problem.
    still = c.get('/api/v1/tasks/' + task['id'], headers=H).json() if _has_task_api(c) else None
    assert still is None or still['state'] == 'claimed'
    assert engine.advance(model)
    assert turn(c, turn_id)['state'] == 'completed'


def _has_task_api(c):
    return c.get('/api/v1/tasks', headers=H).status_code == 200


def test_expiry_is_reported_as_still_running_not_as_failure(env):
    c, engine, app, agent = env
    turn_id = send(c)
    model = FakeModel([call(START_TASK, start_args({'wait_seconds': 1})), final('carry on')])
    engine.advance(model)
    with app.state.transaction() as db:
        db.execute("UPDATE wait_handles SET deadline=0 WHERE turn_id=?", (turn_id,))
    assert engine.advance(model)
    body = turn(c, turn_id)
    seen = json.loads(body['messages'][-2]['content'])
    assert seen['waited_out'] == 'expired'
    assert seen['still_running'] is True
    assert 'failed' not in json.dumps(seen).lower()


def test_check_task_is_immediate_and_needs_no_wait(env):
    c, engine, app, agent = env
    turn_id = send(c)
    engine.advance(FakeModel([call(START_TASK, start_args())]))
    handle = turn(c, turn_id)['wait']['handle']
    engine.advance(FakeModel([call('check_task', {'handle': handle}, 'call-2')]))
    body = turn(c, turn_id)
    # The turn must be runnable again, not parked: a check is a question, not a wait.
    assert body['state'] == 'ready'
    seen = json.loads(body['messages'][-1]['content'])
    assert seen['handle'] == handle and seen['still_running'] is True
    # A status answer is about now, not about a wait that has not ended.
    assert 'waited_out' not in seen


def test_check_task_cannot_reach_another_turns_handle(env):
    """A handle is a label inside one turn, never a key across turns.

    A second turn of the same role waits behind the first (FIFO), so the
    parked turn is the one that asks here. The point is unchanged: a handle is
    validated against the turn asking, and a foreign one reads as unknown.
    """
    c, engine, app, agent = env
    turn_id = send(c)
    engine.advance(FakeModel([call(START_TASK, start_args())]))
    handle = turn(c, turn_id)['wait']['handle']
    with app.state.transaction() as db:
        db.execute("UPDATE wait_handles SET turn_id='someone-else' WHERE id=?", (handle,))
    engine.advance(FakeModel([call('check_task', {'handle': handle}, 'call-9')]))
    seen = json.loads(turn(c, turn_id)['messages'][-1]['content'])
    assert seen['error'] == 'WAIT_HANDLE_UNKNOWN'
    # Nothing about the task leaked through the foreign handle.
    assert 'task_state' not in seen


def test_readonly_turns_get_no_wait_tools():
    """Parking a readonly turn on a write job would be a privilege escalation."""
    from ai_ops.turns import tool_spec
    names = {s['function']['name'] for s in tool_spec(['reader'], mode='readonly')}
    assert not ({START_TASK, 'check_task'} & names)
    write_names = {s['function']['name'] for s in tool_spec(['operator'], mode='direct')}
    assert {START_TASK, 'check_task', PROPOSE_CHANGE} <= write_names


def test_confirm_mode_still_requires_approval_when_not_waiting(env):
    """start_task is not a way round the approval gate."""
    c, engine, app, agent = env
    turn_id = send(c, mode='confirm')
    engine.advance(FakeModel([call(START_TASK, start_args())]))
    body = turn(c, turn_id)
    with app.state.transaction() as db:
        state = db.execute("SELECT state FROM tasks WHERE turn_id=?", (turn_id,)).fetchone()['state']
    assert state == 'awaiting_approval'
    assert 'approval' in json.loads(body['messages'][-1]['content'])['note']
