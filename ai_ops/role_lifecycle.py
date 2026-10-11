"""Archive roles without deleting conversations, providers, or audit evidence."""
import json
import time
from fastapi import Depends, HTTPException

SCHEMA = 'CREATE TABLE IF NOT EXISTS retired_roles(role_id TEXT PRIMARY KEY REFERENCES roles(id), retired_at REAL NOT NULL);'

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
