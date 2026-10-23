"""Drives a fan-out group to completion (§52.4).

Kept apart from `fanout` (pure planning) because this half owns time and
concurrency, which is where the mistakes live.

How it fits the existing pipeline
---------------------------------
A turn waits for exactly one `pending_task_id`. Rather than teach the turn
machinery about N children (which would touch approval, cancellation and audit
in three places), the fan-out gets **one parent task** of its own. The parent is
a normal `tasks` row; its children are ordinary readonly tasks. When the last
child reports, the parent is completed, and the turn wakes up exactly as it does
today.

That keeps the blast radius small, and it keeps a useful property: the parent's
result is the aligned answer, so everything downstream -- scrubbing, audit,
the tool output builder -- sees the same single object it already knows.
"""
from __future__ import annotations

import json
import time
import uuid

from . import fanout

# How often the runner looks for finished children. The runner runs on the
# service's own thread pool; this is a poll interval, not a model-facing wait.
TICK_SECONDS = 0.5


class FanoutRunner:
    def __init__(self, transaction, audit, dispatch_child):
        """``dispatch_child(child_task_id)`` queues one child for execution.

        Injected rather than called directly so the local connector and the
        agent path can share this runner without importing each other, and so
        tests can substitute a synchronous dispatcher.
        """
        self.transaction = transaction
        self.audit = audit
        self.dispatch_child = dispatch_child

    def start(self, db, *, turn, tool, params, asset_ids, rejected, run_as):
        """Create the group plus children and queue everything. Returns group id."""
        group_id = 'fan-' + uuid.uuid4().hex[:16]
        fanout.create(db, group_id=group_id, turn_id=turn['id'], tool=tool,
                      params=params, asset_ids=asset_ids)
        children = []
        for asset_id in asset_ids:
            task_id = str(uuid.uuid4())
            payload = {'kind': 'readonly', 'tool': tool, 'params': params,
                       'run_as': run_as, 'asset_id': asset_id, 'timeout_seconds': 60,
                       'execution_users': json.loads(turn['execution_users']),
                       'role_id': turn['role_id'], 'mode': turn['mode'],
                       'fanout_group': group_id,
                       'idempotency_key': 'fanout:%s:%s' % (group_id, asset_id)}
            # The wire argv is filled in per asset by the dispatcher, because
            # `build` is per-tool not per-asset; the Agent re-derives it anyway
            # and refuses a mismatch (§39.2).
            from .readonly_tools import build
            payload['program'], payload['args'] = build(tool, params)
            db.execute('INSERT INTO tasks(id,role_id,asset_id,payload,state,'
                       'idempotency_key,created_at,updated_at,turn_id) '
                       'VALUES(?,?,?,?,?,?,?,?,?)',
                       (task_id, turn['role_id'], asset_id,
                        json.dumps(payload, ensure_ascii=False), 'queued',
                        payload['idempotency_key'], time.time(), time.time(), turn['id']))
            fanout.attach_task(db, group_id, asset_id, task_id)
            children.append((asset_id, task_id))
        self.audit(db, 'fanout.created', group_id, 'model-tool',
                   {'turn_id': turn['id'], 'tool': tool, 'assets': len(asset_ids),
                    'rejected': rejected})
        return group_id, children

    def status(self, db, group_id):
        """Is the group done? Builds the aligned answer when it is.

        Returns ``(done, answer)``. ``done`` is False while any child is still
        running and the budget has not run out, which is what stops the caller
        from waking the model early and making it believe a partial picture is
        the whole picture.
        """
        group = db.execute('SELECT * FROM fanout_groups WHERE id=?', (group_id,)).fetchone()
        if group is None:
            return True, {'error': 'FANOUT_GROUP_MISSING'}
        children = db.execute('SELECT * FROM fanout_children WHERE group_id=? ORDER BY asset_id',
                              (group_id,)).fetchall()
        leftovers = fanout.pending_children(db, group_id)
        expired = time.time() > group['deadline']
        if leftovers and not expired:
            # Still running. Report progress so a console can show it, but do
            # not hand the model a half-filled answer.
            return False, {'running': len(leftovers), 'of': len(children)}

        # Budget exhausted: whatever has not answered is classified as such and
        # the rest of the group is delivered. Waiting forever on one dead host
        # defeats the purpose of fanning out.
        for child in leftovers:
            fanout.mark(db, group_id, child['asset_id'], 'timeout',
                        'FANOUT_BUDGET_EXHAUSTED')
        # Re-read: `children` was fetched before the marks, and the assembler
        # trusts the marked state (it must -- see fanout.assemble).
        children = db.execute('SELECT * FROM fanout_children WHERE group_id=? ORDER BY asset_id',
                              (group_id,)).fetchall()

        def task_lookup(task_id):
            if not task_id:
                return None
            return db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()

        answer = fanout.assemble(group, children, task_lookup)
        fanout.finish(db, group_id)
        self.audit(db, 'fanout.completed', group_id, 'service',
                   {'summary': answer['summary'], 'assets_total': answer['assets_total'],
                    'budget_exhausted': bool(leftovers)})
        return True, answer

    def child_reported(self, db, task_id):
        """Called when a child task reports. Returns the group id, if any."""
        row = db.execute('SELECT * FROM fanout_children WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            return None
        task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        kind, code, _text = fanout.child_outcome(task)
        fanout.mark(db, row['group_id'], row['asset_id'],
                    'ok' if kind == 'ok' else 'failed', code)
        return row['group_id']
