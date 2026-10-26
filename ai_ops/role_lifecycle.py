"""Archive roles without deleting conversations, providers, or audit evidence.

Role instructions live in their own table (``role_prompts``) rather than as a
column on ``roles``. Two reasons, both about upgrades rather than taste:

1. An existing database gains the table through ``CREATE TABLE IF NOT EXISTS``,
   which is safe to run on every start. ``ALTER TABLE roles`` would need its own
   migration step for every install, and a failed migration on a schema-guarded
   database is the worst possible failure mode.
2. Instructions have their own update time and can be audited, replaced or
   cleared independently of the role row, which is what "who changed how this
   role behaves, and when" requires.
"""
import json
import time
from fastapi import Depends, HTTPException

SCHEMA = ('CREATE TABLE IF NOT EXISTS retired_roles(role_id TEXT PRIMARY KEY REFERENCES roles(id), retired_at REAL NOT NULL);'
          "CREATE TABLE IF NOT EXISTS role_prompts(role_id TEXT PRIMARY KEY REFERENCES roles(id), "
          "instructions TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL);")

# An instruction is capped here as well as in the request model: the model sees
# it on every call, so an unlimited blob is a slow-motion context failure.
MAX_INSTRUCTIONS = 32000


def instructions_for(db, role_id):
    """The role's instructions, or '' when it has none."""
    row = db.execute('SELECT instructions FROM role_prompts WHERE role_id=?', (role_id,)).fetchone()
    return (row['instructions'] if row is not None else '') or ''


def set_instructions(db, role_id, text, actor='admin'):
    text = (text or '').strip()
    if len(text) > MAX_INSTRUCTIONS:
        raise HTTPException(422, 'Role instructions are limited to %d characters' % MAX_INSTRUCTIONS)
    previous = instructions_for(db, role_id)
    db.execute('INSERT INTO role_prompts(role_id,instructions,updated_at) VALUES(?,?,?) '
               'ON CONFLICT(role_id) DO UPDATE SET instructions=excluded.instructions,'
               'updated_at=excluded.updated_at', (role_id, text, time.time()))
    return previous


def _row(row, retired_at, instructions):
    return {'id': row['id'], 'name': row['name'], 'prompt': instructions,
            'has_prompt': bool(instructions.strip()), 'retired_at': retired_at}


def view_rows(db, include_retired=False):
    """Every role with its instructions and archive state.

    ``retired_roles`` uses its own table, so this LEFT JOIN is the only place
    that knows both halves of "does this role still accept work".
    """
    rows = db.execute(
        'SELECT r.*, t.retired_at AS retired_at, p.instructions AS instructions '
        'FROM roles r LEFT JOIN retired_roles t ON t.role_id=r.id '
        'LEFT JOIN role_prompts p ON p.role_id=r.id ORDER BY r.id').fetchall()
    return [_row(r, r['retired_at'], r['instructions'] or '') for r in rows
            if include_retired or r['retired_at'] is None]


def one_view(db, role_id):
    row = db.execute(
        'SELECT r.*, t.retired_at AS retired_at, p.instructions AS instructions '
        'FROM roles r LEFT JOIN retired_roles t ON t.role_id=r.id '
        'LEFT JOIN role_prompts p ON p.role_id=r.id WHERE r.id=?', (role_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Role not found')
    return _row(row, row['retired_at'], row['instructions'] or '')


def require_active(db, role_id):
    if not db.execute('SELECT 1 FROM roles WHERE id=?', (role_id,)).fetchone():
        raise HTTPException(404, 'Role not found')
    if db.execute('SELECT 1 FROM retired_roles WHERE role_id=?', (role_id,)).fetchone():
        raise HTTPException(409, 'Role is archived; no new operations accepted')

def install(app, transaction, audit, admin):
    @app.delete('/api/v1/roles/{role_id}', dependencies=[Depends(admin)])
    def retire(role_id: str):
        with transaction() as db:
            if not db.execute('SELECT 1 FROM roles WHERE id=?', (role_id,)).fetchone():
                raise HTTPException(404, 'Role not found')
            old = db.execute('SELECT * FROM retired_roles WHERE role_id=?', (role_id,)).fetchone()
            if old:
                return {'archived': True, 'retired_at': old['retired_at'], 'history_retained': True}
            if db.execute("SELECT 1 FROM role_turns WHERE role_id=? AND state NOT IN ('completed','failed','cancelled')", (role_id,)).fetchone():
                raise HTTPException(409, 'Finish/cancel active turns and resolve unknown execution before archiving')
            if db.execute('SELECT 1 FROM channel_identities WHERE role_id=?', (role_id,)).fetchone():
                raise HTTPException(409, 'Unbind associated channel identities before archiving')
            for row in db.execute('SELECT config FROM custom_tasks WHERE deleted_at IS NULL'):
                cfg = json.loads(row['config'])
                if cfg['role_id'] == role_id and cfg.get('enabled'):
                    raise HTTPException(409, 'Disable associated custom tasks before archiving')
            now = time.time()
            db.execute('INSERT INTO retired_roles VALUES(?,?)', (role_id, now))
            db.execute('UPDATE channel_pairing SET used_at=? WHERE role_id=? AND used_at IS NULL', (now, role_id))
            audit(db, 'role.archived', role_id, 'admin-token-domain', {'history_retained': True})
            return {'archived': True, 'retired_at': now, 'history_retained': True}
