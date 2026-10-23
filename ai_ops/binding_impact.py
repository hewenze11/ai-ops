""""What will this re-bind actually change?" (§44.3 last clause).

The problem
-----------
A turn freezes its mode and list ids when it starts, so changing a binding
never affects work already running -- that part is already correct and is
tested elsewhere. The gap is the opposite direction: **scheduled tasks have no
frozen binding**. They resolve the binding when they fire, hours or days later,
so a re-bind silently changes what every existing trigger is allowed to do.

That is a real trap. An operator edits the write list at 2pm to remove
``service.stop``, and finds out at 3am that a nightly trigger which used to
restart a service can no longer do it -- or the reverse, that a trigger they
believed was narrowed is now wider because they *added* a rule for something
else.

What this reports
-----------------
Given a mode, the tasks and scheduled triggers that will resolve under it, and
whether the list currently bound is the one they were configured against. It is
a *warning surface*, not an enforcement point: nothing is blocked, because
blocking a re-bind would be worse (the operator usually has a reason).

Why "configured against" is approximated
----------------------------------------
Custom tasks store a mode, not a list id: that is the §44.3 design (lists bind
to modes, tasks bind to modes). So the honest statement is not "this task used
list X" but "this task will use whatever mode ``confirm`` points at when it
fires, which changed N days ago". The report says exactly that rather than
inventing a per-task snapshot.
"""
from __future__ import annotations

import json
import time

from fastapi import Depends


def affected(db, mode: str):
    """Task and trigger counts that resolve under ``mode``, plus last re-bind."""
    binding = db.execute('SELECT * FROM mode_bindings WHERE mode=?', (mode,)).fetchone()
    updated_at = binding['updated_at'] if binding else None

    # Turns still holding this mode that have not finished: they already froze
    # their list ids, so they are listed for completeness but are NOT affected.
    running = db.execute(
        "SELECT COUNT(*) FROM role_turns WHERE mode=? AND state NOT IN "
        "('completed','failed','cancelled','rejected')", (mode,)).fetchone()[0]

    # Custom tasks: the real exposure. They resolve at fire time.
    triggers = []
    for row in db.execute('SELECT id,config,next_fire_at,deleted_at FROM custom_tasks '
                          'WHERE deleted_at IS NULL ORDER BY id'):
        try:
            cfg = json.loads(row['config'])
        except (TypeError, ValueError):
            continue
        if cfg.get('mode') != mode:
            continue
        triggers.append({'custom_task_id': row['id'], 'name': cfg.get('name') or row['id'],
                         'kind': cfg.get('kind'), 'enabled': bool(cfg.get('enabled')),
                         'next_fire_at': row['next_fire_at']})

    return {'mode': mode,
            'bound_lists': {'readonly_list': binding['readonly_list'] if binding else None,
                            'write_list': binding['write_list'] if binding else None,
                            'deny_list': binding['deny_list'] if binding else None},
            'binding_updated_at': updated_at,
            'binding_age_days': (round((time.time() - updated_at) / 86400, 1)
                                 if updated_at else None),
            'affected_triggers': triggers,
            'affected_trigger_count': len(triggers),
            'running_turns_unaffected': running,
            'warning': ('%d scheduled task(s) resolve mode "%s" at fire time. Changing this '
                        'binding changes what they may do on their next run; work already '
                        'running keeps the list it started with.'
                        % (len(triggers), mode)) if triggers else None,
            'note': 'Triggers store a mode, not a list id (§44.3), so they follow the binding '
                    'rather than a snapshot. That is why this warning exists.'}


def stale_bindings(db):
    """Every mode that has triggers depending on it, for one console call."""
    rows = db.execute('SELECT mode FROM mode_bindings ORDER BY mode')
    return {'modes': [affected(db, row['mode']) for row in rows]}


def install(app, transaction, admin):
    @app.get('/api/v1/name-list-bindings/impact', dependencies=[Depends(admin)])
    def impact(mode: str | None = None):
        """Who follows this binding. ``mode`` narrows to one; omitted lists all."""
        with transaction(write=False) as db:
            if mode:
                return affected(db, mode)
            return stale_bindings(db)
