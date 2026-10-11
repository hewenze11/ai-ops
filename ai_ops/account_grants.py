"""Asset-scoped turn authority. Shared administrator trust domain, not human IAM."""
import json
import time
import uuid
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from .models import Identifier, UserName

class Grant(BaseModel):
    model_config = ConfigDict(extra='forbid')
    asset_id: Identifier
    username: UserName

class AccountRequest(Grant):
    reason: str = Field(min_length=1, max_length=2000)

class Answer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: str = Field(pattern='^(approve|reject)$')
    revision: int = Field(ge=0)

SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_authority(
 turn_id TEXT PRIMARY KEY REFERENCES role_turns(id), initial_grants TEXT NOT NULL,
 grants TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS account_requests(
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES role_turns(id),
 tool_call_id TEXT NOT NULL, asset_id TEXT NOT NULL, username TEXT NOT NULL,
 reason TEXT NOT NULL, revision INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 answer TEXT, created_at REAL NOT NULL, answered_at REAL, UNIQUE(turn_id,tool_call_id));
CREATE TABLE IF NOT EXISTS authority_events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, turn_id TEXT NOT NULL REFERENCES role_turns(id),
 revision INTEGER NOT NULL, request_id TEXT NOT NULL UNIQUE, actor TEXT NOT NULL,
 session_ref TEXT NOT NULL, reason TEXT NOT NULL, grant_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS asset_account_notes(
 asset_id TEXT NOT NULL REFERENCES assets(id), username TEXT NOT NULL, note TEXT NOT NULL,
 PRIMARY KEY(asset_id,username));
"""

def validate_grants(db, grants):
    result = [Grant.model_validate(g).model_dump() if not isinstance(g, Grant) else g.model_dump() for g in (grants or [])]
    keys = [(g['asset_id'], g['username']) for g in result]
    if len(keys) != len(set(keys)):
        raise HTTPException(422, 'Duplicate asset account grants')
    for g in result:
        asset = db.execute('SELECT * FROM assets WHERE id=?', (g['asset_id'],)).fetchone()
        if not asset or asset['retired_at'] is not None or g['username'] not in json.loads(asset['allowed_users']):
            raise HTTPException(403, 'Requested account is not registered on the active asset')
    return result

def resolve_input(db, grants, legacy_users):
    """Compatibility input is narrowed once, never a runtime username wildcard.

    Ambiguous names fail. Unregistered names grant nothing. A later registration
    cannot change this frozen snapshot. Persisted legacy rows are NOT resolved.
    """
    if not legacy_users:
        return validate_grants(db, grants)
    if grants or len(set(legacy_users)) != len(legacy_users):
        raise HTTPException(422, 'Use execution_grants alone; duplicate/mixed account input rejected')
    result = []
    assets = db.execute('SELECT * FROM assets WHERE retired_at IS NULL').fetchall()
    for user in legacy_users:
        matches = [a for a in assets if user in json.loads(a['allowed_users'])]
        if len(matches) > 1:
            raise HTTPException(422, 'Ambiguous legacy account: select asset-scoped execution_grants')
        if len(matches) == 1:
            result.append({'asset_id': matches[0]['id'], 'username': user})
    return validate_grants(db, result)

def save_notes(db, asset_id, users, notes):
    if notes is None:
        return
    if any(u not in users or not isinstance(note, str) or len(note) > 500 for u, note in notes.items()):
        raise HTTPException(422, 'Account notes must name registered users and be at most 500 characters')
    db.execute('DELETE FROM asset_account_notes WHERE asset_id=?', (asset_id,))
    db.executemany('INSERT INTO asset_account_notes VALUES(?,?,?)', [(asset_id, u, note) for u, note in notes.items()])

def notes_view(db, asset_id):
    return {r['username']: r['note'] for r in db.execute('SELECT username,note FROM asset_account_notes WHERE asset_id=?', (asset_id,))}

def snapshot(db, turn_id, grants):
    encoded = json.dumps(validate_grants(db, grants))
    db.execute('INSERT INTO turn_authority(turn_id,initial_grants,grants) VALUES(?,?,?)', (turn_id, encoded, encoded))

def authority(db, turn_id):
    row = db.execute('SELECT * FROM turn_authority WHERE turn_id=?', (turn_id,)).fetchone()
    if not row:
        return {'execution_grants': [], 'initial_execution_grants': [], 'authorization_revision': 0, 'legacy_unscoped': True}
    return {'execution_grants': json.loads(row['grants']), 'initial_execution_grants': json.loads(row['initial_grants']), 'authorization_revision': row['revision'], 'legacy_unscoped': False}

def permits(db, turn_id, asset_id, username):
    return {'asset_id': asset_id, 'username': username} in authority(db, turn_id)['execution_grants']

def request_spec():
    return {'type': 'function', 'function': {'name': 'request_execution_account',
      'description': 'Request administrator authorization for one exact asset and OS username in this turn only, with reason. Can also ask for an unregistered account; registration must be done separately before approval. Pauses durably. Does not replace command approval. Never claim approval yourself.',
      'parameters': AccountRequest.model_json_schema()}}

def create_request(db, turn, call, arguments):
    body = AccountRequest.model_validate(arguments)
    if turn['mode'] not in ('direct', 'confirm'):
        raise ValueError('Readonly turns cannot gain execution authority')
    asset = db.execute('SELECT id FROM assets WHERE id=? AND retired_at IS NULL', (body.asset_id,)).fetchone()
    if not asset or not isinstance(call.get('id'), str) or not 1 <= len(call['id']) <= 200:
        raise ValueError('Invalid account request')
    ident = str(uuid.uuid4())
    rev = authority(db, turn['id'])['authorization_revision']
    db.execute('INSERT INTO account_requests(id,turn_id,tool_call_id,asset_id,username,reason,revision,created_at) VALUES(?,?,?,?,?,?,?,?)',
      (ident, turn['id'], call['id'], body.asset_id, body.username, body.reason, rev, time.time()))
    return ident

def requests(db, turn_id):
    return [dict(r) for r in db.execute('SELECT * FROM account_requests WHERE turn_id=? ORDER BY created_at,id', (turn_id,))]

def install(app, transaction, audit, admin):
    @app.post('/api/v1/turns/{turn_id}/account-requests/{request_id}/answer', dependencies=[Depends(admin)])
    def answer(turn_id: str, request_id: str, body: Answer):
        from .turns import set_turn_state
        with transaction() as db:
            turn = db.execute('SELECT * FROM role_turns WHERE id=?', (turn_id,)).fetchone()
            req = db.execute('SELECT * FROM account_requests WHERE id=? AND turn_id=?', (request_id, turn_id)).fetchone()
            if not turn or not req:
                raise HTTPException(404, 'Account request not found')
            if req['state'] == 'answered':
                if req['answer'] != body.decision or req['revision'] != body.revision:
                    raise HTTPException(409, 'Account request already answered differently')
                return {'state': turn['state'], 'duplicate': True, **authority(db, turn_id)}
            auth = authority(db, turn_id)
            if req['state'] != 'pending' or turn['state'] != 'waiting_user' or req['revision'] != body.revision or body.revision != auth['authorization_revision']:
                raise HTTPException(409, 'Account request is no longer current')
            messages = json.loads(turn['messages'])
            if not messages or not any(c.get('id') == req['tool_call_id'] and c.get('function', {}).get('name') == 'request_execution_account' for c in messages[-1].get('tool_calls', [])):
                raise HTTPException(409, 'Pending account tool does not match')
            grant = {'asset_id': req['asset_id'], 'username': req['username']}
            if body.decision == 'approve':
                if turn['mode'] not in ('direct', 'confirm'):
                    raise HTTPException(409, 'Readonly authority cannot be upgraded')
                validate_grants(db, [grant])
                if auth['legacy_unscoped']:
                    snapshot(db, turn_id, [])
                grants = auth['execution_grants']
                if grant not in grants:
                    grants.append(grant)
                rev = auth['authorization_revision'] + 1
                db.execute('UPDATE turn_authority SET grants=?,revision=? WHERE turn_id=?', (json.dumps(grants), rev, turn_id))
                db.execute('INSERT INTO authority_events(turn_id,revision,request_id,actor,session_ref,reason,grant_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
                   (turn_id, rev, request_id, 'admin-token-domain', turn['caller'] or turn['source'], req['reason'], json.dumps(grant), time.time()))
                db.execute('UPDATE role_turns SET execution_users=? WHERE id=?', (json.dumps(sorted({g['username'] for g in grants})), turn_id))
            db.execute("UPDATE account_requests SET state='answered',answer=?,answered_at=? WHERE id=?", (body.decision, time.time(), request_id))
            result = {'decision': body.decision, 'request_id': request_id, 'grant': grant, 'command_approval_granted': False}
            messages.append({'role': 'tool', 'tool_call_id': req['tool_call_id'], 'content': json.dumps(result)})
            db.execute('UPDATE role_turns SET messages=? WHERE id=?', (json.dumps(messages, ensure_ascii=False), turn_id))
            set_turn_state(db, turn_id, 'ready')
            audit(db, 'authority.' + body.decision, turn_id, 'admin-token-domain', {'request_id': request_id, 'reason': req['reason'], 'grant': grant, 'session_ref': turn['caller'], 'revision': authority(db, turn_id)['authorization_revision']})
            return {'state': 'ready', 'duplicate': False, **authority(db, turn_id)}
