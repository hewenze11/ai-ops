"""Execution modes: three built-in behaviours, freely forked into named variants.

A mode is not a label. It decides, for every turn that starts under it, three
things that matter for safety:

* **Which tool surface the model gets.** A read-only mode withholds the change
  tool entirely; there is no command string to inject into. The other two get
  ``propose_change`` and differ only in whether the service queues the change
  or parks it for a human.
* **Which name lists are consulted** when the service validates an action.
* **Whether a proposed change waits.** ``confirm`` parks everything;
  ``direct`` runs only the actions the action table marks auto-approvable, and
  still parks everything else.

The three built-in behaviours are fixed. An operator who needs a different
combination forks one and binds different list versions; the fork inherits the
behaviour and can never change it. That is the whole point: a "fork" that could
also flip ``confirm`` into ``direct`` would be a way to grant authority by
renaming something, so the derived mode carries ``base_behavior`` and the
service always reads *that*, never a copy of it.

Built-in modes cannot be edited or deleted. Forks cannot be forked again, cannot
change behaviour, and cannot be deleted while a schedule or trigger still names
them — a deleted mode must never silently become "confirm" for a nightly job.
"""
from __future__ import annotations

import json
import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

# The behaviours the service actually implements. A mode row stores one of these
# in ``base_behavior``; nothing else in the codebase branches on a mode *name*.
BEHAVIORS = ('readonly', 'confirm', 'direct')

# Legacy names accepted from older clients. 'readonly'/'confirm'/'direct' are
# both the behaviour and the id of the built-in mode that implements them, so
# old payloads keep working unchanged.
LEGACY_MODE_IDS = BEHAVIORS

BUILTIN_MODES = (
    {'id': 'readonly', 'name': '只读模式', 'base_behavior': 'readonly',
     'description': '模型只能读取，没有任何变更工具。'},
    {'id': 'confirm', 'name': '审计模式', 'base_behavior': 'confirm',
     'description': '每个变更都要人工确认后才执行。'},
    {'id': 'direct', 'name': '放行模式', 'base_behavior': 'direct',
     'description': '已标记为安全的变更直接执行，其余仍需人工确认。'},
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_modes(
 id TEXT PRIMARY KEY, name TEXT NOT NULL,
 base_mode TEXT,
 base_behavior TEXT NOT NULL,
 description TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mode_list_bindings(
 mode_id TEXT PRIMARY KEY REFERENCES execution_modes(id),
 readonly_list TEXT, write_list TEXT, deny_list TEXT, updated_at REAL NOT NULL);
"""


class ModeBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=2000)
    readonly_list: str | None = Field(default=None, max_length=200)
    write_list: str | None = Field(default=None, max_length=200)
    deny_list: str | None = Field(default=None, max_length=200)


class ForkBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=2000)
    # Omitted means "inherit the parent's current bindings", which is what an
    # operator expects from "fork and then adjust one thing".
    readonly_list: str | None = Field(default=None, max_length=200)
    write_list: str | None = Field(default=None, max_length=200)
    deny_list: str | None = Field(default=None, max_length=200)


def _slug(name):
    # Derive a readable id, then guarantee uniqueness with the timestamp below.
    cleaned = ''.join(ch if ch.isalnum() or ch in '-_' else '-' for ch in name.strip().lower())
    cleaned = '-'.join(part for part in cleaned.split('-') if part)[:40]
    return (cleaned or 'mode') + '-' + ('%x' % int(time.time() * 1000))[-8:]


def seed(db):
    """Insert the three built-ins once. Never overwrites an operator's row."""
    now = time.time()
    for spec in BUILTIN_MODES:
        db.execute('INSERT INTO execution_modes(id,name,base_mode,base_behavior,description,created_at) '
                   'VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                   (spec['id'], spec['name'], None, spec['base_behavior'], spec['description'], now))


def rows(db):
    return db.execute('SELECT * FROM execution_modes ORDER BY created_at, id').fetchall()


def view(db, row):
    binding = db.execute('SELECT * FROM mode_list_bindings WHERE mode_id=?', (row['id'],)).fetchone()
    return {'id': row['id'], 'name': row['name'], 'base_mode': row['base_mode'],
            'base_behavior': row['base_behavior'],
            'builtin': row['base_mode'] is None,
            'forkable': row['base_mode'] is None,
            'description': row['description'], 'created_at': row['created_at'],
            'readonly_list': binding['readonly_list'] if binding else None,
            'write_list': binding['write_list'] if binding else None,
            'deny_list': binding['deny_list'] if binding else None}


def all_modes(db):
    return [view(db, row) for row in rows(db)]


def one(db, mode_id):
    row = db.execute('SELECT * FROM execution_modes WHERE id=?', (mode_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Mode not found: ' + mode_id)
    return row


def behavior_of(db, mode_id):
    """The behaviour a turn started under this mode actually gets.

    A mode that cannot be resolved falls back to ``readonly``, the strictest
    behaviour: it withholds the change tool entirely. Falling back to
    ``confirm`` would hand a turn a write surface it was never granted, and
    falling back to ``direct`` would turn a typo or a missing row into
    automatic execution. The three built-in ids are also valid behaviours, so
    they resolve to themselves even before ``seed`` has run.
    """
    try:
        row = db.execute('SELECT base_behavior FROM execution_modes WHERE id=?', (mode_id,)).fetchone()
    except Exception:
        row = None
    if row is not None and row['base_behavior'] in BEHAVIORS:
        return row['base_behavior']
    return mode_id if mode_id in BEHAVIORS else 'readonly'


def _check_list(db, list_id):
    if list_id is None:
        return
    row = db.execute('SELECT 1 FROM name_lists WHERE id=? AND retired_at IS NULL', (list_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Unknown name list: ' + list_id)


def _bind(db, mode_id, readonly_list, write_list, deny_list):
    db.execute('INSERT INTO mode_list_bindings(mode_id,readonly_list,write_list,deny_list,updated_at) '
               'VALUES(?,?,?,?,?) ON CONFLICT(mode_id) DO UPDATE SET '
               'readonly_list=excluded.readonly_list, write_list=excluded.write_list, '
               'deny_list=excluded.deny_list, updated_at=excluded.updated_at',
               (mode_id, readonly_list, write_list, deny_list, time.time()))


def fork(db, parent_id, body):
    """Create a derived mode. Behaviour is inherited and immutable."""
    parent = one(db, parent_id)
    if parent['base_mode'] is not None:
        # One level only: a chain of forks makes "what does this actually do"
        # a graph walk, and the behaviour is already fixed at the first fork.
        raise HTTPException(409, 'A derived mode cannot be forked again; fork a built-in mode instead')
    name = body.name.strip()
    if db.execute('SELECT 1 FROM execution_modes WHERE name=?', (name,)).fetchone():
        raise HTTPException(409, 'A mode with this name already exists')
    current = view(db, parent)
    readonly_list = body.readonly_list if body.readonly_list is not None else current['readonly_list']
    write_list = body.write_list if body.write_list is not None else current['write_list']
    deny_list = body.deny_list if body.deny_list is not None else current['deny_list']
    for value in (readonly_list, write_list, deny_list):
        _check_list(db, value)
    new_id = _slug(name)
    db.execute('INSERT INTO execution_modes(id,name,base_mode,base_behavior,description,created_at) '
               'VALUES(?,?,?,?,?,?)',
               (new_id, name, parent_id, parent['base_behavior'],
                (body.description or ('继承自 ' + parent['name']))[:2000], time.time()))
    _bind(db, new_id, readonly_list, write_list, deny_list)
    return view(db, one(db, new_id))


def update(db, mode_id, body):
    """Edit a derived mode's name and bindings. Built-ins are read-only."""
    row = one(db, mode_id)
    if row['base_mode'] is None:
        raise HTTPException(409, 'Built-in modes are fixed; fork one to change its list bindings')
    name = body.name.strip()
    clash = db.execute('SELECT id FROM execution_modes WHERE name=? AND id<>?', (name, mode_id)).fetchone()
    if clash is not None:
        raise HTTPException(409, 'A mode with this name already exists')
    for value in (body.readonly_list, body.write_list, body.deny_list):
        _check_list(db, value)
    db.execute('UPDATE execution_modes SET name=?, description=? WHERE id=?',
               (name, body.description[:2000], mode_id))
    _bind(db, mode_id, body.readonly_list, body.write_list, body.deny_list)
    return view(db, one(db, mode_id))


def delete(db, mode_id):
    """Remove a derived mode that nothing still points at."""
    row = one(db, mode_id)
    if row['base_mode'] is None:
        raise HTTPException(409, 'Built-in modes cannot be deleted')
    # A schedule or trigger that names a deleted mode would run under 'confirm'
    # next time -- a silent behaviour change on unattended work. Refuse instead.
    for task in db.execute('SELECT id,config FROM custom_tasks WHERE deleted_at IS NULL'):
        cfg = json.loads(task['config'])
        if cfg.get('mode_id') == mode_id and cfg.get('enabled'):
            raise HTTPException(409, 'Mode is used by enabled custom task ' + str(task['id']))
    active = db.execute("SELECT COUNT(*) FROM role_turns WHERE mode=? AND state NOT IN "
                        "('completed','failed','cancelled')", (mode_id,)).fetchone()[0]
    if active:
        raise HTTPException(409, 'Mode is in use by %d unfinished turn(s)' % active)
    db.execute('DELETE FROM mode_list_bindings WHERE mode_id=?', (mode_id,))
    db.execute('DELETE FROM execution_modes WHERE id=?', (mode_id,))
    return {'deleted': mode_id}


def routes(app, transaction, audit, admin):
    @app.get('/api/v1/modes', dependencies=[Depends(admin)])
    def list_modes():
        # Writable on purpose: the three built-ins are seeded lazily so a
        # database created before this table existed gains them on first use.
        with transaction() as db:
            seed(db)
            return all_modes(db)

    @app.get('/api/v1/modes/{mode_id}', dependencies=[Depends(admin)])
    def get_mode(mode_id: str):
        with transaction() as db:
            seed(db)
            return view(db, one(db, mode_id))

    @app.post('/api/v1/modes/{mode_id}/fork', dependencies=[Depends(admin)], status_code=201)
    def fork_mode(mode_id: str, body: ForkBody):
        with transaction() as db:
            mode = fork(db, mode_id, body)
            audit(db, 'mode.forked', mode['id'], 'admin',
                  {'parent': mode_id, 'base_behavior': mode['base_behavior']})
            return mode

    @app.put('/api/v1/modes/{mode_id}', dependencies=[Depends(admin)])
    def edit_mode(mode_id: str, body: ModeBody):
        with transaction() as db:
            mode = update(db, mode_id, body)
            audit(db, 'mode.updated', mode_id, 'admin',
                  {'name': mode['name'], 'readonly_list': mode['readonly_list'],
                   'write_list': mode['write_list'], 'deny_list': mode['deny_list']})
            return mode

    @app.delete('/api/v1/modes/{mode_id}', dependencies=[Depends(admin)])
    def remove_mode(mode_id: str):
        with transaction() as db:
            result = delete(db, mode_id)
            audit(db, 'mode.deleted', mode_id, 'admin', {})
            return result
