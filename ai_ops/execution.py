"""Protocol 1.1 execution telemetry, cooperative cancellation and byte archives."""
import base64
import binascii
import hashlib
import json
import time
from typing import Literal

from fastapi import Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

CHUNK_BYTES = 65536
ARCHIVE_BYTES = 64 * 1024 * 1024
# Wire protocols whose agents support telemetry, cancellation and archives.
TELEMETRY_PROTOCOLS = ('1.1', '1.2')
SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_presence(
 asset_id TEXT PRIMARY KEY REFERENCES assets(id), instance_id TEXT NOT NULL,
 agent_version TEXT NOT NULL, protocol_version TEXT NOT NULL, last_seen REAL NOT NULL,
 busy INTEGER NOT NULL DEFAULT 0, busy_by TEXT, busy_task TEXT);
CREATE TABLE IF NOT EXISTS output_archives(
 task_id TEXT NOT NULL REFERENCES tasks(id), stream TEXT NOT NULL,
 size INTEGER NOT NULL DEFAULT 0, sha256 TEXT, complete INTEGER, finalized INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(task_id,stream));
CREATE TABLE IF NOT EXISTS output_chunks(
 task_id TEXT NOT NULL, stream TEXT NOT NULL, offset INTEGER NOT NULL, data BLOB NOT NULL,
 PRIMARY KEY(task_id,stream,offset), FOREIGN KEY(task_id,stream) REFERENCES output_archives(task_id,stream));
"""


def migrate(db):
    columns = {r[1] for r in db.execute('PRAGMA table_info(tasks)')}
    for name, kind in [('claimed_protocol', 'TEXT'), ('cancel_requested_at', 'REAL')]:
        if name not in columns:
            db.execute('ALTER TABLE tasks ADD COLUMN ' + name + ' ' + kind)
    presence = {r[1] for r in db.execute('PRAGMA table_info(agent_presence)')}
    for name, kind in [('busy', 'INTEGER NOT NULL DEFAULT 0'), ('busy_by', 'TEXT'), ('busy_task', 'TEXT')]:
        if name not in presence:
            db.execute('ALTER TABLE agent_presence ADD COLUMN ' + name + ' ' + kind)


def cancel_task(db, audit, task):
    if task['state'] in ('queued', 'awaiting_approval'):
        db.execute("UPDATE tasks SET state='cancelled',updated_at=? WHERE id=?", (time.time(), task['id']))
        return 'cancelled'
    if task['state'] == 'claimed':
        if task['claimed_protocol'] not in TELEMETRY_PROTOCOLS:
            raise HTTPException(409, 'Running cancellation requires protocol 1.1 Agent; execution unchanged')
        if task['cancel_requested_at'] is None:
            db.execute('UPDATE tasks SET cancel_requested_at=?,updated_at=? WHERE id=?', (time.time(), time.time(), task['id']))
            audit(db, 'task.cancel_requested', task['id'], 'admin', {})
        return 'cancel_requested'
    if task['state'] == 'cancelled':
        return 'cancelled'
    raise HTTPException(409, 'Execution is terminal or unknown; cancellation cannot establish its outcome')


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Heartbeat(Strict):
    instance_id: str = Field(min_length=1, max_length=100)
    agent_version: str = Field(min_length=1, max_length=100)
    protocol_version: Literal['1.1', '1.2']
    # Protocol 1.2: the agent reports which controller currently owns the
    # machine's single execution slot, so every other controller can queue
    # instead of racing. Absent on 1.0/1.1 agents (treated as idle).
    busy: bool = False
    busy_by: str | None = Field(default=None, max_length=64)
    busy_task: str | None = Field(default=None, max_length=128)


class Control(Strict):
    claim_id: str = Field(min_length=16, max_length=128)


class Chunk(Control):
    offset: int = Field(ge=0, le=ARCHIVE_BYTES)
    data: str = Field(min_length=4, max_length=87384)


class Finalize(Control):
    size: int = Field(ge=0, le=ARCHIVE_BYTES)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    complete: bool


def check_archives(db, task_id, archives):
    if archives is None:
        return
    if set(archives) != {'stdout', 'stderr'}:
        raise HTTPException(422, 'Both output archives must be referenced')
    for stream, ref in archives.items():
        row = db.execute('SELECT * FROM output_archives WHERE task_id=? AND stream=?', (task_id, stream)).fetchone()
        if row is None or not row['finalized'] or ref != {'size': row['size'], 'sha256': row['sha256'], 'complete': bool(row['complete'])}:
            raise HTTPException(409, 'Output archive is not finalized or does not match')


def install_execution(app, transaction, audit, admin, agent_auth, policy=None):
    def owned(db, asset_id, task_id, authorization, claim_id):
        agent_auth(db, asset_id, authorization)
        task = db.execute('SELECT * FROM tasks WHERE id=? AND asset_id=?', (task_id, asset_id)).fetchone()
        import hmac
        if task is None:
            raise HTTPException(404, 'Task not found')
        if not task['claim_id'] or not hmac.compare_digest(task['claim_id'], claim_id):
            raise HTTPException(403, 'Claim does not match')
        if task['claimed_protocol'] not in TELEMETRY_PROTOCOLS:
            raise HTTPException(409, 'Protocol 1.1 claim required')
        return task

    @app.post('/api/v1/agents/{asset_id}/heartbeat')
    def heartbeat(asset_id: str, body: Heartbeat, authorization: str | None = Header(default=None)):
        with transaction() as db:
            agent_auth(db, asset_id, authorization)
            old = db.execute('SELECT * FROM agent_presence WHERE asset_id=?', (asset_id,)).fetchone()
            now = time.time()
            db.execute('INSERT INTO agent_presence(asset_id,instance_id,agent_version,protocol_version,last_seen,busy,busy_by,busy_task) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET instance_id=excluded.instance_id,agent_version=excluded.agent_version,protocol_version=excluded.protocol_version,last_seen=excluded.last_seen,busy=excluded.busy,busy_by=excluded.busy_by,busy_task=excluded.busy_task', (asset_id, body.instance_id, body.agent_version, body.protocol_version, now, 1 if body.busy else 0, body.busy_by, body.busy_task))
            if old is None or old['instance_id'] != body.instance_id or now - old['last_seen'] > 30:
                audit(db, 'agent.connected', asset_id, 'agent:' + asset_id, body.model_dump())
            # A host that just came back clears the offline debounce, so it can
            # emit a fresh offline event only after it goes silent again.
            if old is not None and now - old['last_seen'] > 30:
                from .asset_lifecycle import note_seen
                db.execute('DELETE FROM asset_offline_events WHERE asset_id=?', (asset_id,))
            return {'accepted': True, 'server_time': now, 'offline_after_seconds': 30}

    def busy_owner(db, asset_id, now):
        """Return (busy_by, busy_task) when the asset's live agent is executing.

        Only a live presence (heartbeat within 30s) counts; a stale row never
        blocks claim, otherwise a dead agent would wedge the queue forever.
        """
        row = db.execute('SELECT * FROM agent_presence WHERE asset_id=?', (asset_id,)).fetchone()
        if row is None or not row['busy'] or now - row['last_seen'] > 30:
            return None, None
        return row['busy_by'], row['busy_task']

    @app.get('/api/v1/agents/{asset_id}/status', dependencies=[Depends(admin)])
    def status(asset_id: str):
        with transaction() as db:
            if not db.execute('SELECT 1 FROM assets WHERE id=?', (asset_id,)).fetchone():
                raise HTTPException(404, 'Asset not found')
            row = db.execute('SELECT * FROM agent_presence WHERE asset_id=?', (asset_id,)).fetchone()
            return {'asset_id': asset_id, 'online': bool(row and time.time() - row['last_seen'] <= 30), 'presence': dict(row) if row else None,
                    'unfinished_tasks': [dict(r) for r in db.execute("SELECT id,state,cancel_requested_at FROM tasks WHERE asset_id=? AND state IN ('claimed','unknown') ORDER BY seq", (asset_id,))]}

    @app.post('/api/v1/agents/{asset_id}/tasks/{task_id}/control')
    def control(asset_id: str, task_id: str, body: Control, authorization: str | None = Header(default=None)):
        with transaction() as db:
            task = owned(db, asset_id, task_id, authorization, body.claim_id)
            return {'cancel_requested': task['cancel_requested_at'] is not None, 'state': task['state']}

    @app.post('/api/v1/agents/{asset_id}/tasks/{task_id}/output/{stream}/chunks')
    def upload(asset_id: str, task_id: str, stream: Literal['stdout', 'stderr'], body: Chunk, authorization: str | None = Header(default=None)):
        try:
            data = base64.b64decode(body.data, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(422, 'Invalid base64')
        if not 1 <= len(data) <= CHUNK_BYTES or body.offset + len(data) > ARCHIVE_BYTES:
            raise HTTPException(413, 'Output archive limit exceeded')
        with transaction() as db:
            task = owned(db, asset_id, task_id, authorization, body.claim_id)
            existing = db.execute('SELECT data FROM output_chunks WHERE task_id=? AND stream=? AND offset=?', (task_id, stream, body.offset)).fetchone()
            if existing:
                if bytes(existing['data']) != data:
                    raise HTTPException(409, 'Chunk offset reused with different bytes')
                return {'accepted': True, 'next_offset': body.offset + len(data), 'duplicate': True}
            if task['state'] != 'claimed':
                raise HTTPException(409, 'Task no longer claimed')
            db.execute('INSERT OR IGNORE INTO output_archives(task_id,stream) VALUES(?,?)', (task_id, stream))
            row = db.execute('SELECT * FROM output_archives WHERE task_id=? AND stream=?', (task_id, stream)).fetchone()
            if row['finalized'] or row['size'] != body.offset:
                raise HTTPException(409, 'Archive finalized or offset is not contiguous')
            if policy is not None:
                from .retention import budget_guard
                within, detail = budget_guard(db, policy, len(data), task_id)
                if not within:
                    raise HTTPException(413, 'Output store is at capacity; prune or raise the quota before uploading more')
            db.execute('INSERT INTO output_chunks VALUES(?,?,?,?)', (task_id, stream, body.offset, data))
            db.execute('UPDATE output_archives SET size=size+? WHERE task_id=? AND stream=?', (len(data), task_id, stream))
            audit(db, 'output.chunk', task_id, 'agent:' + asset_id, {'stream': stream, 'offset': body.offset, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
            return {'accepted': True, 'next_offset': body.offset + len(data), 'duplicate': False}

    @app.post('/api/v1/agents/{asset_id}/tasks/{task_id}/output/{stream}/finalize')
    def finalize(asset_id: str, task_id: str, stream: Literal['stdout', 'stderr'], body: Finalize, authorization: str | None = Header(default=None)):
        with transaction() as db:
            task = owned(db, asset_id, task_id, authorization, body.claim_id)
            row = db.execute('SELECT * FROM output_archives WHERE task_id=? AND stream=?', (task_id, stream)).fetchone()
            if row and row['finalized']:
                if (row['size'], row['sha256'], bool(row['complete'])) != (body.size, body.sha256, body.complete):
                    raise HTTPException(409, 'Different archive already finalized')
                return {'accepted': True, 'duplicate': True}
            if task['state'] != 'claimed':
                raise HTTPException(409, 'Task no longer claimed')
            if (row['size'] if row else 0) != body.size:
                raise HTTPException(409, 'Archive size mismatch')
            digest = hashlib.sha256()
            for chunk in db.execute('SELECT data FROM output_chunks WHERE task_id=? AND stream=? ORDER BY offset', (task_id, stream)):
                digest.update(chunk['data'])
            if digest.hexdigest() != body.sha256:
                raise HTTPException(409, 'Archive digest mismatch')
            db.execute('INSERT INTO output_archives VALUES(?,?,?,?,?,1) ON CONFLICT(task_id,stream) DO UPDATE SET sha256=excluded.sha256,complete=excluded.complete,finalized=1', (task_id, stream, body.size, body.sha256, int(body.complete)))
            audit(db, 'output.finalized', task_id, 'agent:' + asset_id, {'stream': stream, 'size': body.size, 'sha256': body.sha256, 'complete': body.complete})
            return {'accepted': True, 'duplicate': False}

    @app.get('/api/v1/tasks/{task_id}/output', dependencies=[Depends(admin)])
    def output_manifest(task_id: str):
        with transaction() as db:
            if not db.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone():
                raise HTTPException(404, 'Task not found')
            return [dict(r) for r in db.execute('SELECT * FROM output_archives WHERE task_id=? ORDER BY stream', (task_id,))]

    @app.get('/api/v1/tasks/{task_id}/output/{stream}', dependencies=[Depends(admin)])
    def output_bytes(task_id: str, stream: Literal['stdout', 'stderr'], offset: int = 0, limit: int = CHUNK_BYTES):
        if offset < 0 or not 1 <= limit <= CHUNK_BYTES:
            raise HTTPException(422, 'Invalid byte pagination')
        with transaction() as db:
            row = db.execute('SELECT * FROM output_archives WHERE task_id=? AND stream=?', (task_id, stream)).fetchone()
            if row is None:
                raise HTTPException(404, 'Archive not found')
            data = bytearray()
            for chunk in db.execute('SELECT offset,data FROM output_chunks WHERE task_id=? AND stream=? AND offset<? AND offset+length(data)>? ORDER BY offset', (task_id, stream, offset + limit, offset)):
                start = max(0, offset - chunk['offset'])
                end = min(len(chunk['data']), offset + limit - chunk['offset'])
                data.extend(chunk['data'][start:end])
            return Response(bytes(data), media_type='application/octet-stream', headers={'X-Output-Size': str(row['size']), 'X-Next-Offset': str(offset + len(data)), 'X-Output-Finalized': str(bool(row['finalized'])).lower()})
