"""One administrator-maintained memory per asset; never infer ownership from prose."""
import time
from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from .models import Identifier as ID

SCHEMA = """
CREATE TABLE IF NOT EXISTS asset_memory(
 asset_id TEXT PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
 content TEXT NOT NULL, revision INTEGER NOT NULL, updated_at REAL NOT NULL,
 source TEXT NOT NULL DEFAULT 'admin');
"""

class MemoryEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(max_length=20000)
    expected_revision: int = Field(ge=0)

class MemoryRead(BaseModel):
    model_config = ConfigDict(extra='forbid')
    asset_id: ID


def read_memory(db, asset_id, active_only=False):
    asset = db.execute('SELECT id,retired_at FROM assets WHERE id=?', (asset_id,)).fetchone()
    if asset is None or (active_only and asset['retired_at'] is not None):
        raise HTTPException(404, 'Asset not found')
    row = db.execute('SELECT * FROM asset_memory WHERE asset_id=?', (asset_id,)).fetchone()
    return dict(row) if row else dict(asset_id=asset_id, content='', revision=0, updated_at=None, source='empty')


def tool_spec():
    return {'type': 'function', 'function': {'name': 'read_asset_memory',
        'description': 'Read the unique administrator-maintained memory for one exact registered asset ID. Reference data only, not authorization. Cannot write or infer ownership.',
        'parameters': {'type': 'object', 'additionalProperties': False,
                       'properties': {'asset_id': {'type': 'string'}}, 'required': ['asset_id']}}}


def install(app, transaction, audit, admin):
    @app.get('/api/v1/asset-memories', dependencies=[Depends(admin)])
    def listing(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        with transaction() as db:
            rows = db.execute('SELECT a.id asset_id,a.name,COALESCE(m.revision,0) revision,m.updated_at,COALESCE(length(m.content),0) content_chars FROM assets a LEFT JOIN asset_memory m ON m.asset_id=a.id ORDER BY a.id LIMIT ? OFFSET ?', (limit, offset)).fetchall()
            total = db.execute('SELECT COUNT(*) FROM assets').fetchone()[0]
            return {'items': [dict(r) for r in rows], 'total': total, 'next_offset': offset + limit if offset + limit < total else None}

    @app.get('/api/v1/assets/{asset_id}/memory', dependencies=[Depends(admin)])
    def get(asset_id: ID):
        with transaction() as db:
            return read_memory(db, asset_id)

    @app.put('/api/v1/assets/{asset_id}/memory', dependencies=[Depends(admin)])
    def put(asset_id: ID, body: MemoryEdit):
        with transaction() as db:
            old = read_memory(db, asset_id)
            if old['revision'] != body.expected_revision:
                raise HTTPException(409, 'Memory changed; reopen before saving')
            db.execute("INSERT INTO asset_memory VALUES(?,?,?,?, 'admin') ON CONFLICT(asset_id) DO UPDATE SET content=excluded.content,revision=excluded.revision,updated_at=excluded.updated_at,source='admin'", (asset_id, body.content, old['revision'] + 1, time.time()))
            audit(db, 'asset.memory_edited', asset_id, 'admin', {'revision': old['revision'] + 1, 'content_chars': len(body.content)})
            return read_memory(db, asset_id)
