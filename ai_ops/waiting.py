"""Wait handles: let a turn run something long without blocking on it (§52.5).

The scenario
------------
A build takes fifteen minutes. Meanwhile the turn has diagnosis to do, and the
user wants to be able to stop waiting at any moment.

Why this cannot ride on ``waiting_tool``
----------------------------------------
A turn in ``waiting_tool`` is parked until one task settles; the user can only
cancel the whole turn. That is right for reads (they take seconds, and a turn
cancelled during one has nothing worth keeping) but wrong for a long job: the
work is fine, only the *waiting* should end.

So a second parked state exists, ``waiting_task``, and it holds a handle rather
than a task id. A handle can be resolved in exactly three ways:

* **done**      -- the job settled; its outcome becomes tool output.
* **abandoned** -- the user pressed "stop waiting"; the turn resumes *now* and
  the model is told the job is still running. This is not an error and not a
  timeout: the job is fine, the waiting ended.
* **expired**   -- the deadline passed. Also not a failure of the job.

What the model gets on resume
-----------------------------
Never a bare "timeout". A bare failure makes the model retry the build or
declare it broken. It gets a structured body with ``still_running: true``, the
task id, and one instruction: do not start it again, check it with
``check_task`` or carry on.

Boundaries that are not up for negotiation
------------------------------------------
* The job itself is an ordinary write task. Waiting does not widen authority:
  the handle is validated against this turn's own ``pending_task_id``, and the
  underlying task still goes through approval and the policy engine.
* ``check_task`` is immediate and read-only. It never blocks -- a "sleep then
  check" loop would be the same blocking problem wearing a different name.
* An abandoned task keeps running. Abandoning is a statement about attention,
  not about the host.
"""
from __future__ import annotations

import json
import time
import uuid

from .turns import set_turn_state

STATE = 'waiting_task'
DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 3600

START_TASK = 'start_task'
CHECK_TASK = 'check_task'


class StartRejected(Exception):
    """The handle could not be issued. Reported to the model as a refusal."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS wait_handles(
 id TEXT PRIMARY KEY,
 turn_id TEXT NOT NULL,
 task_id TEXT NOT NULL,
 asset_id TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'waiting',
 deadline REAL NOT NULL,
 created_at REAL NOT NULL,
 resolved_at REAL,
 reason TEXT);
CREATE INDEX IF NOT EXISTS wait_handles_turn ON wait_handles(turn_id);
CREATE INDEX IF NOT EXISTS wait_handles_task ON wait_handles(task_id);
"""


def migrate(db):
    db.executescript(SCHEMA)


def tool_specs():
    """Both tools, in the shape the model sees.

    ``start_task`` takes no execution arguments on purpose: the model describes
    *what to run*, never *how long to wait* or *which host to hold open*.
    """
    return [
        {'type': 'function', 'function': {
            'name': START_TASK,
            'description': (
                'Start a write action and keep working while it runs. Use this for '
                'anything that takes minutes (builds, restarts with draining, batch '
                'jobs) instead of propose_change, which makes you wait. Returns a '
                'handle immediately. The job keeps running even if you stop waiting. '
                'It still needs approval exactly like propose_change.'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'asset_id': {'type': 'string'},
                    'run_as': {'type': 'string'},
                    'reason': {'type': 'string',
                               'description': 'One sentence on why this is needed.'},
                    'actions': {'type': 'array', 'minItems': 1, 'maxItems': 10,
                                'items': {'type': 'object', 'additionalProperties': False,
                                          'properties': {'action': {'type': 'string'},
                                                         'params': {'type': 'object'}},
                                          'required': ['action', 'params']}},
                },
                'required': ['asset_id', 'run_as', 'actions']},
        }},
        {'type': 'function', 'function': {
            'name': CHECK_TASK,
            'description': (
                'Check the job you started with start_task. Returns immediately: '
                'still running, or its result. Safe to call as often as you like.'),
            'parameters': {
                'type': 'object',
                'properties': {'handle': {'type': 'string',
                                          'description': 'The handle from start_task.'}},
                'required': ['handle']},
        }},
    ]


def _wake(db, handle, reason, task_lookup):
    """Resume the turn and tell it how the wait ended. Idempotent.

    Written as one guarded UPDATE so two paths (the poller and the user's
    "stop waiting") can race without both resuming the turn or a second
    resume silently overwriting the first transcript append.

    The body is built *after* the UPDATE, from the row as it now is. Building it
    before would mean ``reason`` is still NULL and the model is told the wait
    never ended -- exactly the silence this whole module forbids.
    """
    now = time.time()
    changed = db.execute(
        "UPDATE wait_handles SET state='resolved',reason=?,resolved_at=? "
        "WHERE id=? AND state='waiting'", (reason, now, handle['id'])).rowcount
    if not changed:
        return False
    body = describe(db, handle['id'], task_lookup)
    turn = db.execute('SELECT * FROM role_turns WHERE id=?', (handle['turn_id'],)).fetchone()
    if turn is None or turn['state'] != STATE:
        return False
    messages = json.loads(turn['messages'])
    # The tool call being answered is always the last message: the turn cannot
    # have moved on while it was parked on this handle.
    calls = (messages[-1].get('tool_calls') or [{}]) if messages else [{}]
    messages.append({'role': 'tool', 'tool_call_id': calls[0].get('id'),
                     'content': json.dumps(body, ensure_ascii=False)})
    set_turn_state(db, turn['id'], 'ready')
    db.execute('UPDATE role_turns SET messages=?,pending_task_id=NULL WHERE id=?',
               (json.dumps(messages, ensure_ascii=False), turn['id']))
    return True


def issue(db, *, turn, task, asset_id, seconds):
    """Record a handle for a task this turn just started."""
    handle_id = 'wait-' + uuid.uuid4().hex[:12]
    deadline = time.time() + seconds
    db.execute('INSERT INTO wait_handles(id,turn_id,task_id,asset_id,state,deadline,'
               'created_at) VALUES(?,?,?,?,?,?,?)',
               (handle_id, turn['id'], task['id'], asset_id, 'waiting', deadline, time.time()))
    return handle_id


def describe(db, handle_id, task_lookup):
    """Everything the model is told about a handle, in every outcome.

    One builder for all three endings keeps the wording honest: whichever way
    the wait ends, the model sees the same fields and therefore cannot mistake
    "I stopped waiting" for "it failed".
    """
    handle = db.execute('SELECT * FROM wait_handles WHERE id=?', (handle_id,)).fetchone()
    if handle is None:
        return {'handle': handle_id, 'error': 'WAIT_HANDLE_UNKNOWN'}
    task = task_lookup(handle['task_id'])
    state = task['state'] if task is not None else 'missing'
    body = {'handle': handle_id, 'task_id': handle['task_id'],
            'asset_id': handle['asset_id'], 'task_state': state,
            'waited_out': handle['reason'],
            'elapsed_seconds': round((handle['resolved_at'] or time.time()) - handle['created_at'])}
    if state in ('succeeded', 'failed'):
        body['status'] = state
        return body
    body['still_running'] = True
    body['status'] = 'running'
    body['note'] = ('The job is still running on the host. Do not start it again. '
                    'Use check_task with this handle later, or do other work now.'
                    if state in ('queued', 'claimed', 'awaiting_approval')
                    else 'The job is not running any more; state is %r -- inspect it '
                         'before retrying.' % state)
    return body


def resolve_ready(db, task_lookup, audit):
    """Wake every turn whose job finished or whose deadline passed.

    Called by the turn loop, in the same place that advances fan-out groups, so
    there is one place where "a turn moves forward on its own" lives.
    """
    now = time.time()
    rows = db.execute("SELECT * FROM wait_handles WHERE state='waiting'").fetchall()
    woken = 0
    for handle in rows:
        task = task_lookup(handle['task_id'])
        # A task that vanished is not a running task: say so instead of waiting
        # for a deadline that will tell the model even less.
        finished = task is None or task['state'] in ('succeeded', 'failed', 'cancelled', 'unknown')
        expired = now >= handle['deadline']
        if not (finished or expired):
            continue
        # Only a turn parked on a handle may be woken by it. A turn that checked
        # its job with check_task and moved on is 'ready' or 'calling'; waking it
        # would drop a tool result into a conversation that already answered
        # that call.
        parked = db.execute('SELECT state FROM role_turns WHERE id=?', (handle['turn_id'],)).fetchone()
        if parked is None or parked['state'] != STATE:
            continue
        reason = 'finished' if finished else 'expired'
        if _wake(db, handle, reason, task_lookup):
            audit(db, 'wait.resolved', handle['id'], 'service',
                  {'turn_id': handle['turn_id'], 'task_id': handle['task_id'],
                   'reason': reason, 'task_state': task['state'] if task is not None else 'missing'})
            woken += 1
    return woken


def parked_on_start_task(messages):
    """True when the last thing the model did was ``start_task``.

    That is the shape of a healthy parked turn: the model started a job and the
    turn is waiting for it. Anything else means the model has spoken *since*.

    When there is no assistant message at all, nothing has been asked and the
    answer is True -- waiting is the default, not an event.
    """
    for message in reversed(messages):
        if message.get('role') != 'assistant':
            continue
        calls = message.get('tool_calls') or []
        return bool(calls) and (calls[0].get('function') or {}).get('name') == START_TASK
    return True


def open_handle(db, turn_id):
    """The handle this turn still owes an answer to, if any.

    Used at the moment the model stops talking: if a job is still out there, the
    turn parks on it rather than completing with a promise to follow up.
    """
    return db.execute("SELECT * FROM wait_handles WHERE turn_id=? AND state='waiting' "
                      "ORDER BY created_at DESC LIMIT 1", (turn_id,)).fetchone()


def _answer_call_id(messages, handle):
    """The id of the check_task call answering ``handle``, or None.

    The transcript is the record; scanning backwards finds the most recent
    question about this handle, which is the one being answered.
    """
    for message in reversed(messages):
        for call in reversed(message.get('tool_calls') or []):
            if (call.get('function') or {}).get('name') != CHECK_TASK:
                continue
            try:
                asked = json.loads(call['function'].get('arguments') or '{}').get('handle')
            except (ValueError, KeyError):
                continue
            if asked == handle:
                return call.get('id')
    return None


def release_for_check(db, turn_id, task_lookup):
    """Unpark a turn that has asked something since it was parked.

    A turn in ``waiting_task`` is parked on a *handle*, not on a task. The
    transcript is not "hanging on a tool call" the way ``waiting_tool`` is:
    ``start_task`` already appended its own answer. So the only thing that can
    make the turn runnable again is a *new* model step after parking.

    In practice that step is ``check_task`` -- the one call that is meaningful
    from a parked turn, and the one the model uses instead of sleeping. Its
    answer requires no waiting, so the turn becomes runnable immediately. The
    handle is untouched: a deadline or a finished job still resolves it later.

    Returns True when the turn was released.
    """
    turn = db.execute('SELECT * FROM role_turns WHERE id=?', (turn_id,)).fetchone()
    if turn is None or turn['state'] != STATE:
        return False
    messages = json.loads(turn['messages'])
    if parked_on_start_task(messages):
        # The model has not seen the handle yet -- its last act was asking for
        # it. Release the turn so the *next* step is that model call, reading
        # the handle as tool output. Without this the handle can only reach the
        # model through a task settling, and "keep working while it runs" would
        # be unreachable in practice.
        db.execute("UPDATE role_turns SET state='ready' WHERE id=?", (turn_id,))
        db.execute("UPDATE trigger_events SET state='ready' WHERE turn_id=?", (turn_id,))
        return True
    body = {'note': ('This is a status check, not a wait. The job keeps running; '
                     'call check_task again later or do other work now.')}
    body.update(handlers_check(db, turn_id, messages, task_lookup))
    messages.append({'role': 'tool',
                     'tool_call_id': _answer_call_id(messages, body.get('handle') or ''),
                     'content': json.dumps(body, ensure_ascii=False)})
    db.execute("UPDATE role_turns SET messages=?,state='ready' WHERE id=?",
               (json.dumps(messages, ensure_ascii=False), turn_id))
    db.execute("UPDATE trigger_events SET state='ready' WHERE turn_id=?", (turn_id,))
    return True


def abandon(db, turn_id, audit):
    """The user pressed "stop waiting". Returns False when nothing was waiting.

    A turn parked on a handle is released, not cancelled: the job is untouched.
    A turn parked on an ordinary read keeps the old behaviour (caller cancels).
    """
    handle = db.execute("SELECT * FROM wait_handles WHERE turn_id=? AND state='waiting'",
                        (turn_id,)).fetchone()
    if handle is None:
        return False
    task_lookup = lambda task_id: db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if _wake(db, handle, 'abandoned', task_lookup):
        audit(db, 'wait.abandoned', handle['id'], 'admin',
              {'turn_id': turn_id, 'task_id': handle['task_id']})
        return True
    return False


def handlers_check(db, turn_id, messages, task_lookup):
    """The live status body for whatever handle the parked turn last asked about.

    There is at most one handle per turn in practice, and the question asked
    from a parked turn can only be about a handle this turn owns -- a handle
    of some other turn reads as unknown, which is exactly the answer to give.
    """
    handle_id = ''
    for message in reversed(messages):
        for call in message.get('tool_calls') or []:
            if (call.get('function') or {}).get('name') != CHECK_TASK:
                continue
            try:
                arguments = json.loads(call['function'].get('arguments') or '{}')
            except (ValueError, KeyError):
                arguments = {}
            handle_id = str((arguments or {}).get('handle') or '')
            break
        if handle_id:
            break
    body = status(db, handle_id, turn_id, task_lookup)
    body.pop('waited_out', None)
    return body


def status(db, handle_id, turn_id, task_lookup):
    """Immediate, non-blocking status for ``check_task``."""
    handle = db.execute('SELECT * FROM wait_handles WHERE id=? AND turn_id=?',
                        (handle_id, turn_id)).fetchone()
    if handle is None:
        return {'handle': handle_id, 'error': 'WAIT_HANDLE_UNKNOWN',
                'note': 'No such handle in this turn. Use the handle returned by start_task.'}
    result = describe(db, handle_id, task_lookup)
    # A check answers "where is it now", so it reports the live state rather than
    # the historic reason the wait once ended. Useful output, not a re-litigation
    # of a wait that is already over.
    result.pop('waited_out', None)
    return result
