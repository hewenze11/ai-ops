"""Multi-controller federation: one asset, several control planes (protocol 1.2).

One execution agent may serve several controllers. The agent runs one command
at a time and reports the busy owner; the other controllers must see an empty
claim and queue their own work instead of racing.
"""
from test_control import env, submit, claim, finish  # noqa: F401


def heartbeat(c, tokens, body, asset='a'):
    return c.post('/api/v1/agents/%s/heartbeat' % asset, headers=tokens[asset], json=body)


def claim_as(c, tokens, controller, asset='a'):
    return c.post('/api/v1/agents/%s/claim' % asset, headers=tokens[asset],
                  json={'protocol_version': '1.2', 'controller': controller})


def claim_task(env, controller=None):
    """Claim with an explicit controller name, returning the task payload."""
    c, _, tokens, _ = env
    body = {'protocol_version': '1.2'}
    if controller is not None:
        body['controller'] = controller
    return c.post('/api/v1/agents/a/claim', headers=tokens['a'], json=body).json()['task']


def busy_heartbeat(c, tokens, controller, task_id='t-1', asset='a'):
    return heartbeat(c, tokens, {
        'instance_id': 'inst-1', 'agent_version': 'test', 'protocol_version': '1.2',
        'busy': True, 'busy_by': controller, 'busy_task': task_id}, asset=asset)


def test_busy_agent_makes_other_controller_queue(env):
    c, h, tokens, _ = env
    submit(env)                       # one queued task for this asset
    busy_heartbeat(c, tokens, 'team-b')
    # Team-b is busy; team-a must not be handed the task.
    assert claim_as(c, tokens, 'team-a').json()['task'] is None
    # The busy owner itself is never blocked by its own busy flag.
    assert claim_as(c, tokens, 'team-b').json()['task'] is not None


def test_stale_busy_presence_does_not_wedge_queue(env):
    c, _, tokens, path = env
    submit(env)
    busy_heartbeat(c, tokens, 'team-b')
    import sqlite3
    with sqlite3.connect(path) as db:
        db.execute('UPDATE agent_presence SET last_seen=0')   # agent went silent
    # A dead agent must never block the queue forever.
    assert claim_as(c, tokens, 'team-a').json()['task'] is not None


def test_idle_agent_releases_other_controller(env):
    c, _, tokens, _ = env
    submit(env)
    busy_heartbeat(c, tokens, 'team-b')
    assert claim_as(c, tokens, 'team-a').json()['task'] is None
    # Busy cleared -> team-a can claim again.
    heartbeat(c, tokens, {'instance_id': 'inst-1', 'agent_version': 'test',
                          'protocol_version': '1.2', 'busy': False})
    assert claim_as(c, tokens, 'team-a').json()['task'] is not None


def test_legacy_claim_without_controller_unaffected(env):
    # Protocol 1.0/1.1 agents send no controller; a busy flag from a 1.2 agent
    # must not block a legacy claim (they do not know about federation).
    c, _, tokens, _ = env
    submit(env)
    busy_heartbeat(c, tokens, 'team-b')
    assert claim(env).json()['task'] is not None


def test_busy_flag_persisted_and_exposed_in_status(env):
    c, h, tokens, _ = env
    busy_heartbeat(c, tokens, 'team-b', task_id='abc')
    presence = c.get('/api/v1/agents/a/status', headers=h).json()['presence']
    assert presence['busy'] == 1
    assert presence['busy_by'] == 'team-b'
    assert presence['busy_task'] == 'abc'
