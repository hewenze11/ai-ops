"""Bounded admin-only projections of existing evidence; never rewrite originals."""
import base64
import json
import math
import time
from urllib.parse import quote
from fastapi import Depends, HTTPException
from .scrubbing import redact_structure, redact_text

# SQL union keeps paging bounded and stable across source tables. No output bytes,
# model request bodies or credential-bearing configuration snapshots are selected.
SOURCES = {
 'heartbeat': "SELECT 'heartbeat:'||h.seq id,'heartbeat' kind,h.received_at timestamp,NULL role_id,h.asset_id,h.asset_id entity_id,'heartbeat.received' event,COALESCE(a.name,h.asset_id) summary,json_object('state',h.state,'controller',h.controller,'busy_by',h.busy_by,'protocol',h.protocol,'ttl_seconds',30) details FROM asset_state_samples h LEFT JOIN assets a ON a.id=h.asset_id",
 'task': "SELECT 'task:'||t.id id,'task' kind,t.created_at timestamp,t.role_id,t.asset_id,t.id entity_id,t.state event,t.id summary,'{}' details FROM tasks t",
 'trigger': "SELECT 'trigger:'||e.id id,'trigger' kind,e.created_at timestamp,e.role_id,NULL asset_id,e.id entity_id,e.state event,e.custom_task_id summary,'{}' details FROM trigger_events e",
 'alarm': "SELECT 'alarm:'||a.id id,'alarm' kind,a.received_at timestamp,r.role_id,NULL asset_id,a.id entity_id,a.state event,COALESCE(a.title,a.source) summary,a.payload details FROM alarm_log a LEFT JOIN role_turns r ON r.id=a.turn_id",
 'audit': "SELECT 'audit:'||a.seq id,'audit' kind,a.created_at timestamp,COALESCE(t.role_id,r.role_id,e.role_id) role_id,COALESCE(t.asset_id,x.id) asset_id,a.entity_id,a.event,a.event summary,a.details FROM audit a LEFT JOIN tasks t ON t.id=a.entity_id LEFT JOIN role_turns r ON r.id=a.entity_id LEFT JOIN trigger_events e ON e.id=a.entity_id LEFT JOIN assets x ON x.id=a.entity_id",
 'output': "SELECT 'output:'||t.id id,'output' kind,t.updated_at timestamp,t.role_id,t.asset_id,t.id entity_id,t.state event,'Command output preview' summary,t.result details FROM tasks t WHERE t.result IS NOT NULL",
}


HEARTBEAT_RETENTION_SECONDS = 86400
# The server's agent-presence lease, NOT an agent-reported send interval.
HEARTBEAT_OFFLINE_TTL_SECONDS = 30
STATE_LABELS = {1: '空闲', 2: '本主执行', 3: '他主执行', 4: 'TTL断联', None: '未知'}


def query_heartbeat_records(db, asset_id=None, before=None, limit=100, now=None):
    """Read-only 24h event projection; raw ACK evidence remains in telemetry.

    The trusted protocol-1.2 state samples provide 1/2/3. A gap after such a
    sample yields ONE explicitly derived server-TTL expiry, not fake heartbeats.
    Unknown/legacy samples never produce a state-4 signal. Cursor pins the upper
    observation time and asset filter; original records are never rewritten.
    The caller must apply the same tenant/admin authorization as other logs.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise HTTPException(422, 'Invalid heartbeat pagination')
    current = time.time() if now is None else now
    as_of, boundary = current, None
    if before:
        try:
            if len(before) > 1024:
                raise ValueError()
            cursor = json.loads(base64.urlsafe_b64decode(before))
            if not isinstance(cursor, list) or len(cursor) != 4:
                raise ValueError()
            at, identity, snapshot, scoped_asset = cursor
            if (type(at) not in (int, float) or not math.isfinite(at) or
                    type(snapshot) not in (int, float) or not math.isfinite(snapshot) or
                    not isinstance(identity, str) or len(identity) > 100 or
                    scoped_asset != asset_id or at > snapshot or snapshot > current):
                raise ValueError()
            as_of, boundary = snapshot, (at, identity)
        except (ValueError, TypeError, UnicodeError):
            raise HTTPException(422, 'Invalid heartbeat cursor')
    # The extra TTL anchor catches an expiry just inside the retention window.
    # LEAD considers ALL samples (including unknown ones), so a received but
    # unclassifiable heartbeat correctly interrupts a potential disconnect gap.
    sql = '''WITH samples AS (
        SELECT h.*, a.name asset_name,
               LEAD(h.received_at) OVER (PARTITION BY h.asset_id ORDER BY h.received_at,h.seq) next_at
        FROM asset_state_samples h JOIN assets a ON a.id=h.asset_id
        WHERE h.received_at>=? AND h.received_at<=?
          AND a.connection_type='agent' AND (? IS NULL OR h.asset_id=?)
    ), events AS (
        SELECT 'heartbeat:'||seq id, seq, asset_id, asset_name, received_at timestamp,
               received_at observed_at,
               CASE WHEN protocol='1.2' AND state IN (1,2,3) THEN state ELSE NULL END state,
               controller,busy_by,protocol,'received_heartbeat' source
        FROM samples
        UNION ALL
        SELECT 'heartbeat-ttl:'||seq id, seq, asset_id, asset_name, received_at+? timestamp,
               received_at observed_at, 4 state, controller,busy_by,protocol,
               'observed_ttl_expiry' source
        FROM samples WHERE protocol='1.2' AND state IN (1,2,3)
          AND COALESCE(next_at,?)>received_at+?
    ) SELECT * FROM events WHERE timestamp>=? AND timestamp<=?'''
    ttl = HEARTBEAT_OFFLINE_TTL_SECONDS
    # Retention is enforced even when no new heartbeat has caused pruning.
    cutoff = max(as_of - HEARTBEAT_RETENTION_SECONDS, current - HEARTBEAT_RETENTION_SECONDS)
    params = [cutoff-ttl, as_of, asset_id, asset_id, ttl, as_of, ttl, cutoff, as_of]
    if boundary:
        sql += ' AND (timestamp<? OR (timestamp=? AND id<?))'
        params.extend([boundary[0], boundary[0], boundary[1]])
    sql += ' ORDER BY timestamp DESC,id DESC LIMIT ?'
    params.append(limit + 1)
    rows = [dict(row) for row in db.execute(sql, params)]
    more, rows = len(rows) > limit, rows[:limit]
    for row in rows:
        row['state_code'] = row['state']
        row['state_label'] = STATE_LABELS[row['state']]
        row['state_known'] = row['state'] is not None
        row['derived'] = row['source'] == 'observed_ttl_expiry'
        row['ttl_seconds'] = ttl if row['state'] is not None else None
        row['ttl_source'] = 'server_presence_lease' if row['state'] is not None else None
        row['ack_detail_path'] = '/api/v1/assets/' + quote(row['asset_id'], safe='') + '/heartbeats?view=attempts'
        for key in ('asset_name', 'controller', 'busy_by'):
            if row[key] is not None:
                row[key] = redact_text(row[key])
    cursor = (base64.urlsafe_b64encode(json.dumps([
        rows[-1]['timestamp'], rows[-1]['id'], as_of, asset_id
    ]).encode()).decode() if more and rows else None)
    return {'items': rows, 'next_before': cursor, 'retention_hours': 24,
            'as_of': as_of, 'ttl_seconds': ttl, 'order': 'newest_first'}


def install_unified_logs(app, transaction, admin):
    @app.get('/api/v1/logs/heartbeats', dependencies=[Depends(admin)])
    def heartbeat_records(asset_id: str | None = None, before: str | None = None, limit: int = 100):
        with transaction() as db:
            return query_heartbeat_records(db, asset_id=asset_id, before=before, limit=limit)

    @app.get('/api/v1/logs', dependencies=[Depends(admin)])
    def logs(kind: str = 'all', role_id: str | None = None, asset_id: str | None = None,
             before: str | None = None, limit: int = 100):
        if kind not in ('all', *SOURCES) or not 1 <= limit <= 200:
            raise HTTPException(422, 'Invalid kind or pagination')
        boundary = None
        if before:
            try:
                if len(before) > 512:
                    raise ValueError()
                boundary = json.loads(base64.urlsafe_b64decode(before))
                if not isinstance(boundary, list) or len(boundary) != 2 or not isinstance(boundary[0], (float, int)) or not isinstance(boundary[1], str):
                    raise ValueError()
            except Exception:
                raise HTTPException(422, 'Invalid log cursor')
        # Heartbeats have their own compact table. Explicit kind=heartbeat is
        # retained for existing clients, but routine arrivals do not flood all.
        selected = [source for name, source in SOURCES.items() if name != 'heartbeat'] if kind == 'all' else [SOURCES[kind]]
        sql = 'SELECT * FROM (' + ' UNION ALL '.join(selected) + ') WHERE 1=1'
        params = []
        for column, value in [('role_id', role_id), ('asset_id', asset_id)]:
            if value is not None:
                sql += ' AND ' + column + '=?'
                params.append(value)
        if boundary:
            sql += ' AND (timestamp<? OR (timestamp=? AND id<?))'
            params.extend([boundary[0], boundary[0], boundary[1]])
        sql += ' ORDER BY timestamp DESC,id DESC LIMIT ?'
        params.append(limit + 1)
        with transaction() as db:
            rows = [dict(r) for r in db.execute(sql, params)]
        more = len(rows) > limit
        rows = rows[:limit]
        for r in rows:
            try:
                details = json.loads(r['details']) if r['details'] else {}
            except ValueError:
                details = {'text': r['details']}
            if r['kind'] == 'output' and isinstance(details, dict):
                details = {k: (str(v)[:4000] if k in ('stdout', 'stderr') else v) for k,v in details.items() if k in ('status','exit_code','stdout','stderr','error_code','output_truncated','output_archives')}
            r['details'] = redact_structure(details)
            r['summary'] = redact_text(r['summary'] or '')[:1000]
        cursor = base64.urlsafe_b64encode(json.dumps([rows[-1]['timestamp'], rows[-1]['id']]).encode()).decode() if more and rows else None
        return {'items': rows, 'next_before': cursor}
