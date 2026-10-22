"""Mode and name-list binding (§44.3, §43.1).

Two rules drive this module, and both exist to make an audit question answerable
later ("why did the AI do that on this host on that day?"):

1. **A task freezes its mode and the name lists it used.** Changing the mode means
   starting a new task; the old task keeps the old mode. Otherwise a task could
   begin in read-only and silently continue in a writing mode.
2. **A name list that has been referenced is frozen.** Editing it forks a new
   version with a ``parent_id``, so the audit can walk the chain and tell "who
   added which rule, when". The old version stays readable forever.

The list itself carries rules, not just paths; each rule may be scoped to hosts
carrying a tag, so one global list still expresses per-host differences
(§44.1 last clause).

Host tags are stored here too: a rule's scope is meaningless without knowing
which hosts carry which tag, and the policy engine needs that answer at decision
time, not at configuration time.
"""
from __future__ import annotations

import json
import time
import uuid

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .models import Identifier, new_id

# Three lists, one per mode (§44.3). The audit records the id in force, so a
# turn started before a re-bind keeps pointing at the list it actually used.
MODES = ('readonly', 'confirm', 'direct')

LISTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS name_lists(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
 version INTEGER NOT NULL, parent_id TEXT, revision INTEGER NOT NULL DEFAULT 1,
 created_at REAL NOT NULL, retired_at REAL);
CREATE TABLE IF NOT EXISTS name_list_rules(
 list_id TEXT NOT NULL REFERENCES name_lists(id), rule_id TEXT NOT NULL,
 action TEXT NOT NULL, hosts TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(list_id,rule_id));
CREATE TABLE IF NOT EXISTS host_tags(
 asset_id TEXT NOT NULL REFERENCES assets(id), tag TEXT NOT NULL,
 PRIMARY KEY(asset_id,tag));
"""

# Which list kind each mode binds by default. Read-only turns use the readonly
# list; writing modes use the write list. The deny list applies to all modes.
KIND_FOR_MODE = {'readonly': 'readonly', 'confirm': 'write', 'direct': 'write'}

_LIST_KINDS = ('readonly', 'write', 'deny')


def _fork_id():
    return 'list-' + uuid.uuid4().hex[:12]


class Rule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rule_id: str = Field(min_length=1, max_length=100)
    action: str = Field(min_length=1, max_length=100)
    hosts: list[str] = Field(default_factory=list, max_length=50)
    note: str = Field(default='', max_length=500)


class NameList(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(default_factory=_fork_id)
    kind: str = Field(pattern='^(readonly|write|deny)$')
    name: str = Field(min_length=1, max_length=200)
    rules: list[Rule] = Field(default_factory=list, max_length=500)


def create_list(db, body: NameList, actor='admin'):
    """Persist a new list version. Never mutates an existing one."""
    rule_ids = [r.rule_id for r in body.rules]
    if len(rule_ids) != len(set(rule_ids)):
        raise HTTPException(422, 'Duplicate rule ids in one list')
    db.execute('INSERT INTO name_lists(id,kind,name,version,parent_id,revision,created_at) VALUES(?,?,?,?,?,?,?)',
               (body.id, body.kind, body.name, 1, None, 1, time.time()))
    db.executemany('INSERT INTO name_list_rules(list_id,rule_id,action,hosts,note) VALUES(?,?,?,?,?)',
                   [(body.id, r.rule_id, r.action, json.dumps(r.hosts), r.note) for r in body.rules])
    return body.id


def fork_list(db, list_id, edits, actor='admin'):
    """Edit = fork. The old version stays readable and referenced turns keep it."""
    row = db.execute('SELECT * FROM name_lists WHERE id=?', (list_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Name list not found')
    if row['retired_at'] is not None:
        raise HTTPException(409, 'Retired list cannot be forked')
    new_id = _fork_id()
    db.execute('INSERT INTO name_lists(id,kind,name,version,parent_id,revision,created_at) VALUES(?,?,?,?,?,?,?)',
               (new_id, row['kind'], edits.get('name') or row['name'], row['version'] + 1, list_id, 1, time.time()))
    if edits.get('rules') is not None:
        rules = [Rule.model_validate(r) for r in edits['rules']]
        db.executemany('INSERT INTO name_list_rules(list_id,rule_id,action,hosts,note) VALUES(?,?,?,?,?)',
                       [(new_id, r.rule_id, r.action, json.dumps(r.hosts), r.note) for r in rules])
    else:
        db.execute('INSERT INTO name_list_rules(list_id,rule_id,action,hosts,note) SELECT ?,rule_id,action,hosts,note FROM name_list_rules WHERE list_id=?',
                   (new_id, list_id))
    return new_id


def view(db, list_id):
    row = db.execute('SELECT * FROM name_lists WHERE id=?', (list_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Name list not found')
    rules = [{'rule_id': r['rule_id'], 'action': r['action'],
              'hosts': json.loads(r['hosts']), 'note': r['note']}
             for r in db.execute('SELECT * FROM name_list_rules WHERE list_id=? ORDER BY rule_id', (list_id,))]
    return {**{k: row[k] for k in ('id', 'kind', 'name', 'version', 'parent_id', 'created_at', 'retired_at')},
            'rules': rules}


def bindings(db):
    """Current mode -> list mapping. Missing rows mean 'platform default'."""
    rows = db.execute('SELECT * FROM mode_bindings').fetchall()
    return {r['mode']: {'readonly_list': r['readonly_list'], 'write_list': r['write_list'],
                        'deny_list': r['deny_list']} for r in rows}


def snapshot_binding(db, mode):
    """Freeze the (mode, list ids) triple a turn starts under (§43.1, §44.3)."""
    row = db.execute('SELECT * FROM mode_bindings WHERE mode=?', (mode,)).fetchone()
    if row is None:
        return {'mode': mode, 'readonly_list': None, 'write_list': None, 'deny_list': None}
    return {'mode': mode, 'readonly_list': row['readonly_list'],
            'write_list': row['write_list'], 'deny_list': row['deny_list']}


def set_binding(db, mode, readonly_list=None, write_list=None, deny_list=None, actor='admin'):
    """Rebinding only affects new tasks; running tasks keep their snapshot."""
    if mode not in MODES:
        raise HTTPException(422, 'Unknown mode')
    for value in (readonly_list, write_list, deny_list):
        if value is not None:
            row = db.execute('SELECT id FROM name_lists WHERE id=? AND retired_at IS NULL', (value,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Unknown name list: ' + value)
    db.execute('INSERT INTO mode_bindings(mode,readonly_list,write_list,deny_list,updated_at) VALUES(?,?,?,?,?) '
               'ON CONFLICT(mode) DO UPDATE SET readonly_list=excluded.readonly_list,'
               'write_list=excluded.write_list,deny_list=excluded.deny_list,updated_at=excluded.updated_at',
               (mode, readonly_list, write_list, deny_list, time.time()))


def rule_actions(db, list_id, action_hosts=None):
    """Allowed action set for a list, plus per-action host scope from its rules.

    A rule with no host scope applies everywhere. A rule with scope only applies
    on hosts carrying one of those tags, so one global list can still express
    "restart nginx on prod only" without a second list.
    """
    if not list_id:
        return set(), dict(action_hosts or {})
    hosts = dict(action_hosts or {})
    allowed = set()
    for row in db.execute('SELECT action,hosts FROM name_list_rules WHERE list_id=?', (list_id,)):
        action, scope_json = row[0], row[1]
        allowed.add(action)
        scope = json.loads(scope_json or '[]')
        if scope:
            merged = set(hosts.get(action, [])) | set(scope)
            hosts[action] = sorted(merged)
    return allowed, hosts


def tags_for(db, asset_id):
    return sorted(r['tag'] for r in db.execute('SELECT tag FROM host_tags WHERE asset_id=?', (asset_id,)))


def set_tags(db, asset_id, tags):
    clean = sorted({t.strip() for t in tags if t and t.strip()})
    for tag in clean:
        if len(tag) > 100:
            raise HTTPException(422, 'Host tag too long')
    db.execute('DELETE FROM host_tags WHERE asset_id=?', (asset_id,))
    db.executemany('INSERT INTO host_tags(asset_id,tag) VALUES(?,?)', [(asset_id, t) for t in clean])


SCHEMA = LISTS_SCHEMA + """
CREATE TABLE IF NOT EXISTS mode_bindings(
 mode TEXT PRIMARY KEY, readonly_list TEXT, write_list TEXT, deny_list TEXT, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS platform_defaults(
 name TEXT PRIMARY KEY, list_id TEXT NOT NULL, seeded_version TEXT NOT NULL);
"""


# The platform ships one read-only list and one write list. Once seeded they are
# ordinary lists: the user can fork them, but the originals stay referenced by
# seeded history, so they are never mutated in place.
def default_lists():
    from .actions import ACTIONS
    from .turns import READONLY_TOOLS
    write_auto = {name for name, spec in ACTIONS.items() if spec['auto_approvable']}
    return {
        'readonly': NameList(kind='readonly', name='Platform default: read-only tools',
                             rules=[Rule(rule_id='ro-' + tool, action=tool)
                                    for tool in sorted(READONLY_TOOLS)]),
        'write': NameList(kind='write', name='Platform default: change actions',
                          rules=[Rule(rule_id='wr-' + action, action=action,
                                      note='auto in direct mode' if action in write_auto else 'needs a human')
                                 for action in sorted(ACTIONS)]),
    }


def ensure_defaults(db, baseline_version):
    """Seed default lists and bindings exactly once per install.

    Idempotent: a second call finds the marker and does nothing. The marker also
    records which baseline version was current when the lists were seeded, so an
    operator can tell whether the rules predate a platform security update.
    """
    if db.execute("SELECT 1 FROM platform_defaults WHERE name='lists'").fetchone():
        return
    ids = {kind: create_list(db, body) for kind, body in default_lists().items()}
    for mode in MODES:
        set_binding(db, mode, readonly_list=ids['readonly'], write_list=ids['write'])
    db.execute("INSERT INTO platform_defaults(name,list_id,seeded_version) VALUES('lists',?,?)",
               (json.dumps(ids), baseline_version))


def routes(app, transaction, audit, admin):
    """Admin surface for lists, bindings and host tags."""
    @app.get('/api/v1/name-lists', dependencies=[Depends(admin)])
    def list_all():
        with transaction() as db:
            return [view(db, r['id']) for r in db.execute(
                'SELECT id FROM name_lists WHERE retired_at IS NULL ORDER BY created_at DESC')]

    @app.get('/api/v1/name-lists/{list_id}', dependencies=[Depends(admin)])
    def get_one(list_id: str):
        with transaction() as db:
            return view(db, list_id)

    @app.post('/api/v1/name-lists', dependencies=[Depends(admin)], status_code=201)
    def create(body: NameList):
        with transaction() as db:
            list_id = create_list(db, body)
            audit(db, 'name_list.created', list_id, 'admin', body.model_dump())
            return view(db, list_id)

    @app.post('/api/v1/name-lists/{list_id}/fork', dependencies=[Depends(admin)], status_code=201)
    def fork(list_id: str, edits: dict):
        with transaction() as db:
            new_id = fork_list(db, list_id, edits)
            audit(db, 'name_list.forked', new_id, 'admin', {'parent_id': list_id})
            return view(db, new_id)

    # Registered before the {list_id} routes so 'bindings' is never read as an id.
    @app.get('/api/v1/name-list-bindings', dependencies=[Depends(admin)])
    def show_bindings():
        with transaction() as db:
            return bindings(db)

    @app.put('/api/v1/name-list-bindings/{mode}', dependencies=[Depends(admin)])
    def bind(mode: str, body: dict):
        with transaction() as db:
            set_binding(db, mode, body.get('readonly_list'), body.get('write_list'), body.get('deny_list'))
            audit(db, 'name_list.bound', mode, 'admin', body)
            return bindings(db)

    @app.put('/api/v1/assets/{asset_id}/tags', dependencies=[Depends(admin)])
    def put_tags(asset_id: Identifier, body: dict):
        with transaction() as db:
            if db.execute('SELECT 1 FROM assets WHERE id=?', (asset_id,)).fetchone() is None:
                raise HTTPException(404, 'Asset not found')
            set_tags(db, asset_id, body.get('tags') or [])
            audit(db, 'asset.tags', asset_id, 'admin', {'tags': tags_for(db, asset_id)})
            return {'asset_id': asset_id, 'tags': tags_for(db, asset_id)}
