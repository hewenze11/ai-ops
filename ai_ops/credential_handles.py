"""Credential handles, control-plane side (§42, §46.3).

The control plane stores handles: id, name, note, where it was found, which
host, and who may see it. It never stores the value — the value stays on the
managed host, encrypted with a key that host owns (§47.1).

That changes what "credential management" means here. The console is a
registry and an inventory ("17 plaintext secrets found on 4 hosts"), not a
vault. The one exception is the connector path, where there is no agent and the
value must pass through the control plane (§46.1 degraded path); those live in
``connector_secrets`` and are marked as such.

Two rules keep this from becoming a leak channel:
  * The inventory view never includes a value, and there is no API that returns
    one for an agent-managed asset.
  * A handle is scoped to the asset that reported it. A handle from host A is
    not a licence to inject into host B.
"""
from __future__ import annotations

import json
import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field


SCHEMA = """
CREATE TABLE IF NOT EXISTS credential_handles(
 id TEXT NOT NULL, asset_id TEXT NOT NULL REFERENCES assets(id),
 name TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
 kind TEXT NOT NULL DEFAULT 'pattern', created_at REAL NOT NULL, last_seen_at REAL NOT NULL,
 dismissed INTEGER NOT NULL DEFAULT 0, dismissed_reason TEXT,
 PRIMARY KEY(asset_id,id));
CREATE TABLE IF NOT EXISTS credential_events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, asset_id TEXT NOT NULL, credential_id TEXT NOT NULL,
 event TEXT NOT NULL, actor TEXT NOT NULL, detail TEXT NOT NULL, created_at REAL NOT NULL);
"""

# ``kind`` distinguishes what the operator is looking at, because the two need
# different decisions: a pattern hit can be a false positive (a git sha), a path
# rule hit is a whole file the operator declared secret.
KINDS = ('pattern', 'path', 'file', 'connector')


class Handle(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    note: str = Field(default='', max_length=2000)
    source: str = Field(default='', max_length=1000)
    kind: str = Field(default='pattern', pattern='^(pattern|path|file|connector)$')


class Annotation(BaseModel):
    """Operator-editable metadata (§42 item two). id, source and kind are not here
    on purpose: where a secret was found and what matched are evidence, not
    opinion, and renaming must not be able to rewrite them."""
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


def annotate(db, asset_id, credential_id, name=None, note=None, actor='admin'):
    row = db.execute('SELECT name,note FROM credential_handles WHERE asset_id=? AND id=?',
                     (asset_id, credential_id)).fetchone()
    if row is None:
        raise HTTPException(404, 'Credential handle not found')
    new_name = name if name is not None else row['name']
    new_note = note if note is not None else row['note']
    db.execute('UPDATE credential_handles SET name=?,note=? WHERE asset_id=? AND id=?',
               (new_name, new_note, asset_id, credential_id))
    db.execute('INSERT INTO credential_events(asset_id,credential_id,event,actor,detail,created_at) '
               "VALUES(?,?,'annotated',?,?,?)",
               (asset_id, credential_id, actor,
                json.dumps({'name': [row['name'], new_name], 'note_changed': new_note != row['note']}, ensure_ascii=False),
                time.time()))


def register(db, asset_id, handles, actor='agent'):
    """Record what an agent discovered. Idempotent: re-discovery refreshes.

    A dismissed handle stays dismissed. Otherwise the same false positive would
    be re-surfaced on every read and the operator would switch the feature off,
    which is the failure mode §42.1 item 3 warns about.
    """
    now = time.time()
    fresh = []
    for item in handles:
        row = db.execute('SELECT dismissed FROM credential_handles WHERE asset_id=? AND id=?',
                         (asset_id, item['id'])).fetchone()
        if row is None:
            db.execute('INSERT INTO credential_handles(id,asset_id,name,note,source,kind,created_at,last_seen_at) '
                       'VALUES(?,?,?,?,?,?,?,?)',
                       (item['id'], asset_id, item['name'], item.get('note') or '',
                        item.get('source') or '', item.get('kind') or 'pattern', now, now))
            db.execute('INSERT INTO credential_events(asset_id,credential_id,event,actor,detail,created_at) '
                       "VALUES(?,?,'discovered',?,?,?)", (asset_id, item['id'], actor, json.dumps({'kind': item.get('kind') or 'pattern'}), now))
            fresh.append(item['id'])
        else:
            db.execute('UPDATE credential_handles SET last_seen_at=? WHERE asset_id=? AND id=?',
                       (now, asset_id, item['id']))
    return fresh


def listing(db, asset_id=None, include_dismissed=False):
    query = 'SELECT * FROM credential_handles'
    clauses, args = [], []
    if asset_id:
        clauses.append('asset_id=?')
        args.append(asset_id)
    if not include_dismissed:
        clauses.append('dismissed=0')
    if clauses:
        query += ' WHERE ' + ' AND '.join(clauses)
    query += ' ORDER BY last_seen_at DESC'
    return [{k: row[k] for k in ('id', 'asset_id', 'name', 'note', 'source', 'kind',
                                 'created_at', 'last_seen_at', 'dismissed', 'dismissed_reason')}
            for row in db.execute(query, args)]


def dismiss(db, asset_id, credential_id, reason, actor='admin'):
    """False-positive veto (§42.1 item 3). A dismissed handle is not a secret."""
    changed = db.execute('UPDATE credential_handles SET dismissed=1,dismissed_reason=? '
                         'WHERE asset_id=? AND id=?', (reason[:500], asset_id, credential_id)).rowcount
    if not changed:
        raise HTTPException(404, 'Credential handle not found')
    db.execute('INSERT INTO credential_events(asset_id,credential_id,event,actor,detail,created_at) '
               "VALUES(?,?,'dismissed',?,?,?)", (asset_id, credential_id, actor, json.dumps({'reason': reason[:500]}), time.time()))


def may_inject(db, asset_id, credential_id):
    """A handle authorizes injection on the host that reported it, and only there."""
    row = db.execute('SELECT dismissed FROM credential_handles WHERE asset_id=? AND id=?',
                     (asset_id, credential_id)).fetchone()
    return bool(row) and not row[0]


def install(app, transaction, audit, admin):
    @app.get('/api/v1/credentials', dependencies=[Depends(admin)])
    def all_handles(asset_id: str | None = None, include_dismissed: bool = False):
        with transaction(write=False) as db:
            rows = listing(db, asset_id, include_dismissed)
            summary = {}
            for row in rows:
                summary.setdefault(row['asset_id'], 0)
                summary[row['asset_id']] += 1
            return {'items': rows, 'counts_by_asset': summary,
                    'note': '仅返回凭证记录，不返回明文；Agent 凭证留在被管主机，SSH 登录凭证由控制端连接器保存。'}

    @app.get('/api/v1/assets/{asset_id}/credentials', dependencies=[Depends(admin)])
    def asset_handles(asset_id: str, include_dismissed: bool = False):
        with transaction(write=False) as db:
            return {'items': listing(db, asset_id, include_dismissed)}

    @app.post('/api/v1/assets/{asset_id}/credentials', dependencies=[Depends(admin)], status_code=201)
    def accept(asset_id: str, body: Handle):
        with transaction() as db:
            if db.execute('SELECT 1 FROM assets WHERE id=?', (asset_id,)).fetchone() is None:
                raise HTTPException(404, 'Asset not found')
            fresh = register(db, asset_id, [body.model_dump()], actor='admin')
            audit(db, 'credential.registered', asset_id, 'admin', {'credential_id': body.id})
            return {'created': bool(fresh)}

    @app.post('/api/v1/assets/{asset_id}/credentials/{credential_id}/dismiss', dependencies=[Depends(admin)])
    def reject(asset_id: str, credential_id: str, body: dict):
        with transaction() as db:
            dismiss(db, asset_id, credential_id, str(body.get('reason') or 'not a secret'))
            audit(db, 'credential.dismissed', asset_id, 'admin', {'credential_id': credential_id})
            return {'dismissed': True}

    @app.put('/api/v1/assets/{asset_id}/credentials/{credential_id}', dependencies=[Depends(admin)])
    def rename(asset_id: str, credential_id: str, body: Annotation):
        with transaction() as db:
            annotate(db, asset_id, credential_id, body.name, body.note)
            audit(db, 'credential.annotated', asset_id, 'admin', {'credential_id': credential_id})
            return next(h for h in listing(db, asset_id, include_dismissed=True) if h['id'] == credential_id)

    @app.get('/api/v1/assets/{asset_id}/credentials/{credential_id}/history', dependencies=[Depends(admin)])
    def history(asset_id: str, credential_id: str):
        with transaction(write=False) as db:
            return [{k: row[k] for k in ('seq', 'event', 'actor', 'detail', 'created_at')}
                    for row in db.execute('SELECT * FROM credential_events WHERE asset_id=? AND credential_id=? ORDER BY seq',
                                          (asset_id, credential_id))]
