"""Operator inbox: everything the AI is waiting on a human for (§44, stage 6).

One list, one mental model: "the model stopped and wants my decision". The
four kinds of pause the engine knows about are surfaced side by side so the
operator never has to know which table a pause lives in:

  * approvals        -- a change set in ``awaiting_approval`` (gate 3/4)
  * questions        -- ``ask_user`` business decisions (data, not authority)
  * account_requests -- ``request_execution_account`` (authority, per turn)

Rejecting a change set is deliberately NOT the same as cancelling the turn.
Cancel ends the conversation; reject returns ``{"status":"rejected","reason"}``
to the model as the tool result so it can read why and propose something else.
The operator's reason is therefore part of the model's context and must be
written for the model, not for the audit log (the audit log gets it too).

Nothing here widens authority: the only state transitions are
awaiting_approval -> queued (approve) and awaiting_approval -> rejected.
"""
from __future__ import annotations

import json
import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .scrubbing import redact_structure


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    # Shown to the model on reject, kept in the audit trail on both paths.
    reason: str = Field(default='', max_length=2000)


def _approval_view(db, row):
    payload = json.loads(row['payload'])
    turn = db.execute('SELECT prompt,mode,source,caller FROM role_turns WHERE id=?', (row['turn_id'],)).fetchone() if row['turn_id'] else None
    asset = db.execute('SELECT name FROM assets WHERE id=?', (row['asset_id'],)).fetchone()
    role = db.execute('SELECT name FROM roles WHERE id=?', (row['role_id'],)).fetchone()
    steps = payload.get('steps') or []
    return redact_structure({
        'kind': 'approval', 'task_id': row['id'], 'turn_id': row['turn_id'],
        'asset_id': row['asset_id'], 'asset_name': asset['name'] if asset else None,
        'role_id': row['role_id'], 'role_name': role['name'] if role else None,
        'run_as': payload.get('run_as'), 'mode': payload.get('mode'),
        # What the operator is actually approving: the exact argv per step and
        # the hash the Agent will re-check before running it (§44 lesson: every
        # byte that executes is inside this digest).
        'steps': [{'action': s.get('action'), 'program': s.get('program'), 'args': s.get('args'),
                   'params': s.get('params'), 'timeout_seconds': s.get('timeout_seconds')} for s in steps],
        'payload_sha256': (payload.get('approval') or {}).get('payload_sha256'),
        'reason': payload.get('reason') or '',
        'prompt': (turn['prompt'][:500] if turn else None),
        'caller': (turn['caller'] or turn['source']) if turn else None,
        'created_at': row['created_at'],
        # Legacy raw-command tasks have no steps; show the command so the card
        # is never empty for an old-style approval.
        'command': payload.get('command') if not steps else None,
    })


def _question_view(db, row):
    role = db.execute('SELECT name FROM roles WHERE id=?', (row['role_id'],)).fetchone()
    return {'kind': 'question', 'question_id': row['id'], 'turn_id': row['turn_id'],
            'role_id': row['role_id'], 'role_name': role['name'] if role else None,
            'question': row['question'], 'options': json.loads(row['options']),
            'created_at': row['created_at']}


def _account_view(db, row):
    turn = db.execute('SELECT role_id,mode FROM role_turns WHERE id=?', (row['turn_id'],)).fetchone()
    asset = db.execute('SELECT name FROM assets WHERE id=?', (row['asset_id'],)).fetchone()
    role = db.execute('SELECT name FROM roles WHERE id=?', (turn['role_id'],)).fetchone() if turn else None
    return {'kind': 'account_request', 'request_id': row['id'], 'turn_id': row['turn_id'],
            'role_id': turn['role_id'] if turn else None, 'role_name': role['name'] if role else None,
            'asset_id': row['asset_id'], 'asset_name': asset['name'] if asset else None,
            'username': row['username'], 'reason': row['reason'], 'mode': turn['mode'] if turn else None,
            # The answer endpoint requires the revision it was asked under.
            'revision': row['revision'], 'created_at': row['created_at']}


def inbox(db):
    approvals = [_approval_view(db, r) for r in db.execute(
        "SELECT * FROM tasks WHERE state='awaiting_approval' ORDER BY seq")]
    questions = [_question_view(db, r) for r in db.execute(
        "SELECT q.* FROM turn_questions q JOIN role_turns t ON t.id=q.turn_id "
        "WHERE q.state='pending' AND t.state='waiting_user' ORDER BY q.created_at,q.id")]
    accounts = [_account_view(db, r) for r in db.execute(
        "SELECT a.* FROM account_requests a JOIN role_turns t ON t.id=a.turn_id "
        "WHERE a.state='pending' AND t.state='waiting_user' ORDER BY a.created_at,a.id")]
    # §51.4: masked reads show up as unlock cards in the same list, because
    # "the AI is blocked" and "you may want to see this value" are both
    # "a human decision is pending here".
    from .sensitive import pending as pending_hits
    hits = pending_hits(db)
    return {'approvals': approvals, 'questions': questions, 'account_requests': accounts,
            'sensitive_hits': hits,
            'total': len(approvals) + len(questions) + len(accounts) + len(hits),
            'note': 'Answer endpoints: POST /api/v1/tasks/{task_id}/approve|reject, '
                    '/api/v1/turns/{turn_id}/questions/{question_id}/answer, '
                    '/api/v1/turns/{turn_id}/account-requests/{request_id}/answer, '
                    '/api/v1/sensitive-hits/{hit_id}/unlock'}


def install(app, transaction, audit, admin):
    @app.get('/api/v1/inbox', dependencies=[Depends(admin)])
    def show_inbox():
        with transaction(write=False) as db:
            return inbox(db)

    @app.post('/api/v1/tasks/{task_id}/reject', dependencies=[Depends(admin)])
    def reject(task_id: str, body: Decision | None = None):
        reason = (body.reason if body else '') or 'rejected by operator'
        with transaction() as db:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if task is None:
                raise HTTPException(404, 'Task not found')
            if task['state'] != 'awaiting_approval':
                raise HTTPException(409, 'Task is not awaiting approval')
            # The result is what the model reads back as the tool output. It
            # is a terminal task state, so the turn engine resumes the turn
            # with this message and the model may propose an alternative.
            result = {'status': 'rejected', 'reason': reason, 'by': 'operator',
                      'command_approval_granted': False}
            db.execute("UPDATE tasks SET state='rejected',result=?,updated_at=? WHERE id=?",
                       (json.dumps(result, ensure_ascii=False), time.time(), task_id))
            audit(db, 'task.rejected', task_id, 'admin', {'reason': reason, 'turn_id': task['turn_id']})
        return {'state': 'rejected'}
