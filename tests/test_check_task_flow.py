"""check_task on a parked turn (docs §52.5).

A turn parked on a wait handle is not "hanging on a tool call": ``start_task``
already answered itself, so the parked transcript ends in a ``tool`` message and
the model's next reply is a *new* step. That asymmetry is what these tests pin
down -- the parked turn must become runnable again when the model asks for a
status, without the job being touched.
"""
import json

import pytest
from fastapi.testclient import TestClient

from ai_ops.app import create_app
from ai_ops.turns import START_TASK

ADMIN = 'check-flow-admin-not-real-' + 'z' * 30
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
    return {'message': {'role': 'assistant', 'content': text},
            'usage': {'total_tokens': 5}}


def call(name, arguments, call_id='call-1'):
    return {'message': {'role': 'assistant', 'content': None,
                        'tool_calls': [{'id': call_id, 'type': 'function',
                                        'function': {'name': name,
                                                     'arguments': json.dumps(arguments)}}]},
            'usage': {'total_tokens': 5}}


START_ARGS = {'asset_id': 'host', 'run_as': 'operator', 'reason': 'rebuild',
              'actions': [{'action': 'service.restart', 'params': {'unit': 'nginx'}}]}


@pytest.fixture()
def env(tmp_path):
    app = create_app(str(tmp_path / 'check.db'), ADMIN, 'test-model')
    c = TestClient(app)
    c.post('/api/v1/roles', headers=H, json={'id': 'ops', 'name': 'ops'})
    c.put('/api/v1/roles/ops/model', headers=H, json={'enabled': True})
    asset = c.post('/api/v1/assets', headers=H,
                   json={'id': 'host', 'name': 'Host',
                         'allowed_users': ['operator']}).json()
    agent = {'Authorization': 'Bearer ' + asset['agent_token']}
    return c, app.state.role_engine, app, agent


def send(c, key='message-0001'):
    return c.post('/api/v1/roles/ops/messages', headers=H,
                  json={'text': 'rebuild the service', 'execution_users': ['operator'],
                        'mode': 'direct', 'idempotency_key': key}).json()['turn_id']


def view(c, turn_id):
    return c.get('/api/v1/turns/' + turn_id, headers=H).json()


def test_a_parked_turn_keeps_working_after_a_check(env):
    """The turn is not frozen: the model can ask and carry on in the same flow.

    Before this worked, a parked turn answered check_task never -- the call sat
    in the transcript while the turn stayed parked, so the model's request was
    silently dropped.
    """
    c, engine, app, agent = env
    turn_id = send(c)
    assert engine.advance(FakeModel([call(START_TASK, START_ARGS)]))
    handle = view(c, turn_id)['wait']['handle']

    model = FakeModel([call('check_task', {'handle': handle}, 'call-2'), final('carrying on')])
    assert engine.advance(model), 'a parked turn with a question must advance'
    assert engine.advance(model), 'and the model must be able to carry on afterwards'
    body = view(c, turn_id)
    assert body['state'] == 'completed' and body['final_text'] == 'carrying on'
    # The status answer reached the model in the request after the check.
    second = model.requests[1]['messages']
    assert second[-1]['role'] == 'tool'
    seen = json.loads(second[-1]['content'])
    assert seen['handle'] == handle
    assert seen['still_running'] is True
    assert seen['status'] == 'running'
    assert 'waited_out' not in seen
    # And the job is untouched: still waiting for the agent, not cancelled by
    # the end of the conversation.
    with app.state.transaction() as db:
        task = db.execute('SELECT * FROM tasks WHERE turn_id=?', (turn_id,)).fetchone()
    assert json.loads(task['payload'])['kind'] == 'change'
    assert task['state'] == 'queued'


def test_a_finished_job_beats_a_pending_check(env):
    """If the job settled first, the turn gets the result, not a status ping.

    Both things are true at once; the outcome is what the model needs, and
    reporting "still running" over a finished job would make it wait again.
    """
    c, engine, app, agent = env
    turn_id = send(c)
    assert engine.advance(FakeModel([call(START_TASK, START_ARGS)]))
    task = c.post('/api/v1/agents/host/claim', headers=agent,
                  json={'protocol_version': '1.3'}).json()['task']
    c.post(f"/api/v1/agents/host/tasks/{task['id']}/result", headers=agent,
           json={'claim_id': task['claim_id'], 'status': 'succeeded',
                 'exit_code': 0, 'stdout': 'restarted\n'})
    model = FakeModel([final('it finished')])
    assert engine.advance(model)
    body = view(c, turn_id)
    assert body['state'] == 'completed'
    # The model saw the job's result, marked as such.
    seen = json.loads(model.requests[0]['messages'][-1]['content'])
    assert seen['status'] == 'succeeded'


def test_a_parked_turn_always_gets_to_speak_once(env):
    """Parking hands the handle to the model, it does not freeze the turn.

    Without this step the model never sees the handle it asked for, so
    check_task would be unreachable and "keep working while it runs" would be a
    promise with nothing behind it. The turn runs once, then settles.
    """
    c, engine, app, agent = env
    turn_id = send(c)
    assert engine.advance(FakeModel([call(START_TASK, START_ARGS)]))
    model = FakeModel([final('I will watch it')])
    assert engine.advance(model)
    body = view(c, turn_id)
    assert body['state'] == 'completed'
    # The model was shown the handle before it answered.
    assert json.loads(model.requests[0]['messages'][-1]['content'])['handle']
