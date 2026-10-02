"""Task leases (observation only) and explicit operator resolution of unknown runs."""
import json
import time

from fastapi import Depends, Header, HTTPException
from pydantic import Field

from .models import Identifier, StrictModel

_json = lambda value: json.dumps(value, ensure_ascii=False)
_json_load = json.loads

LEASE_SECONDS = 300
SCHEMA = """
CREATE TABLE IF NOT EXISTS task_leases(
 task_id TEXT PRIMARY KEY REFERENCES tasks(id), asset_id TEXT NOT NULL,
 lease_id TEXT NOT NULL, claimed_at REAL NOT NULL, expires_at REAL NOT NULL, closed_at REAL);
CREATE INDEX IF NOT EXISTS lease_expiry ON task_leases(expires_at);
CREATE TABLE IF NOT EXISTS task_controls(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, event TEXT NOT NULL,
 actor TEXT NOT NULL, details TEXT NOT NULL, created_at REAL NOT NULL);
"""


def migrate(db):
    columns = {r[1] for r in db.execute('PRAGMA table_info(tasks)')}
    if 'lease_seconds' not in columns:
        db.execute('ALTER TABLE tasks ADD COLUMN lease_seconds INTEGER')
    if 'claim_count' not in columns:
        db.execute('ALTER TABLE tasks ADD COLUMN claim_count INTEGER NOT NULL DEFAULT 0')
    if 'last_lease_expired' not in columns:
        db.execute('ALTER TABLE tasks ADD COLUMN last_lease_expired INTEGER NOT NULL DEFAULT 0')


def open_lease(db, task_id, asset_id, lease_id, now=None):
    now = time.time() if now is None else now
    db.execute('INSERT INTO task_leases(task_id,asset_id,lease_id,claimed_at,expires_at) VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET asset_id=excluded.asset_id,lease_id=excluded.lease_id,claimed_at=excluded.claimed_at,expires_at=excluded.expires_at,closed_at=NULL',
               (task_id, asset_id, lease_id, now, now + LEASE_SECONDS))


def close_lease(db, task_id, now=None):
    db.execute('UPDATE task_leases SET closed_at=? WHERE task_id=? AND closed_at IS NULL', (time.time() if now is None else now, task_id))


def lease_view(db, task_id, now=None):
    now = time.time() if now is None else now
    row = db.execute('SELECT * FROM task_leases WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        return None
    return {'lease_id': row['lease_id'], 'expires_at': row['expires_at'], 'expired': row['expires_at'] <= now,
            'closed': row['closed_at'] is not None}


class Resolution(StrictModel):
    action: str = Field(pattern='^(confirm_succeeded|confirm_failed|abandon)$')
    note: str = Field(min_length=3, max_length=2000)
    confirm_task_id: Identifier


def install_leases(app, transaction, audit, admin):
    def control(db, task_id, event, actor, details):
        db.execute('INSERT INTO task_controls(task_id,event,actor,details,created_at) VALUES(?,?,?,?,?)',
                   (task_id, event, actor, _json(details), time.time()))

    def run(action):
        with transaction() as db:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (action.confirm_task_id,)).fetchone()
            if task is None:
                raise HTTPException(404, 'Task not found')
            if task['state'] != 'unknown':
                raise HTTPException(409, 'Only an unknown execution can be resolved; current state is ' + task['state'])
            now = time.time()
            if action.action == 'abandon':
                # Deliberately does NOT mark success or failure: the outcome stays
                # unverified, the role is unblocked, and the ambiguity is recorded.
                db.execute("UPDATE tasks SET state='abandoned',updated_at=? WHERE id=?", (now, task['id']))
                _resolve_turn(db, task, 'completed', None)
            else:
                outcome = 'succeeded' if action.action == 'confirm_succeeded' else 'failed'
                turn_state = 'completed' if outcome == 'succeeded' else 'failed'
                db.execute('UPDATE tasks SET state=?,updated_at=? WHERE id=?', (outcome, now, task['id']))
                _resolve_turn(db, task, turn_state, 'OPERATOR_CONFIRMED_' + outcome.upper() if outcome == 'failed' else None)
            close_lease(db, task['id'], now)
            control(db, task['id'], 'resolution.' + action.action, 'admin', {'note': action.note, 'turn_state': 'completed' if action.action == 'abandon' else turn_state})
            audit(db, 'task.' + action.action, task['id'], 'admin', {'note': action.note, 'previous_state': 'unknown'})
        return {'task_id': task['id'], 'state': action.action}

    def _resolve_turn(db, task, turn_state, error):
        if not task['turn_id']:
            return
        from .turns import set_turn_state
        set_turn_state(db, task['turn_id'], turn_state, error if turn_state != 'completed' else None)

    def resolve_unknown(task_id: Identifier, body: Resolution, authorization: str | None = Header(default=None)):
        admin(authorization)
        if body.confirm_task_id != task_id:
            raise HTTPException(422, 'Task ID mismatch')
        return run(body)

    app.post('/api/v1/tasks/{task_id}/resolve', dependencies=[Depends(admin)])(resolve_unknown)

    @app.get('/api/v1/tasks/{task_id}/controls', dependencies=[Depends(admin)])
    def controls(task_id: Identifier):
        with transaction() as db:
            if not db.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone():
                raise HTTPException(404, 'Task not found')
            return [{**dict(r), 'details': _json_load(r['details'])} for r in db.execute('SELECT * FROM task_controls WHERE task_id=? ORDER BY seq', (task_id,))]

    @app.get('/api/v1/operator/attention', dependencies=[Depends(admin)])
    def attention():
        now = time.time()
        with transaction() as db:
            unknown = [dict(r) for r in db.execute("SELECT id,asset_id,role_id,payload,updated_at FROM tasks WHERE state='unknown' ORDER BY seq")]
            stale = []
            for row in db.execute("SELECT l.*,t.state FROM task_leases l JOIN tasks t ON t.id=l.task_id"):
                if row['closed_at'] is not None or row['expires_at'] > now:
                    continue
                stale.append({'task_id': row['task_id'], 'asset_id': row['asset_id'], 'state': row['state'],
                              'expired_for': round(now - row['expires_at'], 1),
                              'note': 'Stale lease is observation only; the task was not reassigned.'})
            offline = []
            for row in db.execute('SELECT asset_id,last_seen FROM agent_presence'):
                if now - row['last_seen'] > 30:
                    offline.append({'asset_id': row['asset_id'], 'silent_for': round(now - row['last_seen'], 1)})
            return {'unknown_executions': unknown, 'stale_leases': stale, 'offline_assets': offline}
