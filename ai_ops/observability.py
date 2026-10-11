"""Derived inventory and transport-aware health; no synthetic agent heartbeats."""
import json
import time
from fastapi import Depends, HTTPException
from . import scrubbing

SCHEMA = """
CREATE TABLE IF NOT EXISTS heartbeat_samples(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, asset_id TEXT NOT NULL REFERENCES assets(id), received_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS heartbeat_asset_time ON heartbeat_samples(asset_id,received_at);
CREATE TABLE IF NOT EXISTS asset_state_samples(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, asset_id TEXT NOT NULL REFERENCES assets(id),
 received_at REAL NOT NULL, state INTEGER, controller TEXT, busy_by TEXT, protocol TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS asset_state_time ON asset_state_samples(asset_id,received_at);
CREATE TABLE IF NOT EXISTS controller_observations(
 asset_id TEXT PRIMARY KEY REFERENCES assets(id), controller TEXT NOT NULL, observed_at REAL NOT NULL);
"""
from . import heartbeat_telemetry
SCHEMA += heartbeat_telemetry.SCHEMA
RETENTION_SECONDS = 86400
INTERVAL = 10
INVENTORY_ID = 'system-asset-inventory'
LOCAL_SCOPE = {'execution_scope': 'service_runtime', 'execution_environment': 'service_runtime',
               'security_notice': '本地执行未与控制面做权限隔离；服务同账号命令可能读取服务可读凭据或写入数据库。readonly禁止执行，confirm需人工逐条审查，direct不提供此隔离。',
               'scope_notice': '本地执行仅作用于主服务运行环境；Docker部署通常是容器，不代表宿主机可控。',
               'health_notice': '仅表示本地执行worker存活，不代表宿主机权限或网络心跳健康。'}


def heartbeat_sample(db, asset_id, now):
    db.execute('DELETE FROM heartbeat_samples WHERE received_at<?', (now - RETENTION_SECONDS,))
    # Coalesce to 10-second observation windows. Agent cadence varies (idle 3s,
    # running 2s, plus network time); this is NOT a sent-packet denominator.
    last = db.execute('SELECT received_at FROM heartbeat_samples WHERE asset_id=? ORDER BY seq DESC LIMIT 1', (asset_id,)).fetchone()
    if not last or int(last['received_at'] // INTERVAL) != int(now // INTERVAL):
        db.execute('INSERT INTO heartbeat_samples(asset_id,received_at) VALUES(?,?)', (asset_id, now))


def record_state(db, asset_id, body, now):
    observer = db.execute('SELECT controller FROM controller_observations WHERE asset_id=?', (asset_id,)).fetchone()
    controller = body.heartbeat_telemetry.controller if body.heartbeat_telemetry else (observer['controller'] if observer else None)
    code = None
    if body.protocol_version == '1.2':
        if not body.busy:
            code = 1
        elif controller and body.busy_by:
            code = 2 if controller == body.busy_by else 3
    db.execute('INSERT INTO asset_state_samples(asset_id,received_at,state,controller,busy_by,protocol) VALUES(?,?,?,?,?,?)',
               (asset_id, now, code, controller, body.busy_by, body.protocol_version))
    db.execute('DELETE FROM asset_state_samples WHERE received_at<?', (now - RETENTION_SECONDS,))


def state_history(db, aid, now):
    rows = [dict(r) for r in db.execute('SELECT * FROM asset_state_samples WHERE asset_id=? AND received_at>=? ORDER BY seq DESC LIMIT 100', (aid, now-RETENTION_SECONDS))][::-1]
    points = []
    for index, row in enumerate(rows):
        points.append({'at': row['received_at'], 'state': row['state'], 'source': 'received_heartbeat'})
        next_at = rows[index+1]['received_at'] if index+1 < len(rows) else now
        if row['state'] is not None and next_at > row['received_at'] + 30:
            points.append({'at': row['received_at'] + 30, 'state': 4, 'source': 'observed_ttl_expiry'})
    return points


def health_view(db, asset, app, now=None):
    now = time.time() if now is None else now
    aid, transport = asset['id'], asset['connection_type']
    presence = db.execute('SELECT * FROM agent_presence WHERE asset_id=?', (aid,)).fetchone()
    online = bool(presence and now - presence['last_seen'] <= 30)
    state = 'healthy' if online else 'offline'
    source = 'agent_heartbeat'
    heartbeat = {'applicable': transport == 'agent', 'expected_interval_seconds': None,
                 'observation_window_seconds': INTERVAL, 'actual_attempts': 'unavailable',
                 'metric': 'heartbeat_window_arrival', 'metric_label': '最近10个10秒时间窗心跳缺失率（非实际尝试丢失率、非网络丢包率）',
                 'interval_source': 'server_observation_window',
                 'window_size': 10, 'observed_slots': 0, 'successful_slots': 0,
                 'success_ratio': None, 'missed_ratio': None, 'retention_hours': 24}
    if transport == 'agent':
        # Only completed expected wall-clock slots count; duplicates cannot inflate success.
        end = int(now // INTERVAL)
        first = db.execute('SELECT MIN(received_at) FROM heartbeat_samples WHERE asset_id=? AND received_at>=?', (aid, now - RETENTION_SECONDS)).fetchone()[0]
        if first is not None:
            start = max(end - 10, int(first // INTERVAL))
            n = max(0, end - start)
            samples = db.execute('SELECT received_at FROM heartbeat_samples WHERE asset_id=? AND received_at>=? AND received_at<?', (aid, start * INTERVAL, end * INTERVAL)).fetchall()
            success = len({int(r[0] // INTERVAL) for r in samples})
            heartbeat.update(observed_slots=n, successful_slots=success,
                             success_ratio=success/n if n else None, missed_ratio=(n-success)/n if n else None)
        exact = heartbeat_telemetry.view(db, aid, now)
        if exact and (not presence or any(s['session_id'] == presence['instance_id'] for s in exact['streams'])):
            # Keep legacy window fields as-is for existing API clients.
            heartbeat.update(exact)
    elif transport == 'local':
        source = 'local_worker'
        local = db.execute('SELECT enabled FROM local_host WHERE asset_id=?', (aid,)).fetchone()
        worker = getattr(app.state, 'local_worker_thread', None)
        online = bool(local and local['enabled'] and worker and worker.is_alive())
        state = 'disabled' if not local or not local['enabled'] else ('healthy' if online else 'offline')
        presence = None
    else:
        # SSH has no heartbeat; absence of agent_presence is not evidence of failure.
        source, online, state, presence = 'ssh_on_demand', False, 'unknown', None
    queued = db.execute("SELECT COUNT(*) FROM tasks WHERE asset_id=? AND state='queued'", (aid,)).fetchone()[0]
    busy_by = presence['busy_by'] if presence and online and presence['busy'] else None
    busy_task = presence['busy_task'] if presence and online and presence['busy'] else None
    observer = db.execute('SELECT * FROM controller_observations WHERE asset_id=?', (aid,)).fetchone()
    # Controller identity is stable; only LIVE busy presence expires. A long
    # execution may continue heartbeating without making another claim request.
    waiting = bool(queued and busy_by and observer and observer['controller'] != busy_by)
    queue = 'waiting_other_controller' if waiting else ('busy' if presence and online and presence['busy'] else ('idle' if online else ('unknown' if state == 'unknown' else 'offline')))
    history = state_history(db, aid, now) if transport == 'agent' else []
    latest = db.execute('SELECT * FROM asset_state_samples WHERE asset_id=? ORDER BY seq DESC LIMIT 1', (aid,)).fetchone() if transport == 'agent' else None
    state_code = latest['state'] if latest else None
    if state_code is not None and now - latest['received_at'] > 30:
        state_code = 4
    return {'asset_id': aid, 'connection_type': transport, 'online': online, 'health_state': state,
            'execution_state': state_code, 'state_history': history, 'offline_ttl_seconds': 30,
            'observing_controller': scrubbing.redact_text(latest['controller']) if latest and latest['controller'] else None,
            'health_source': source, 'presence': dict(presence) if presence else None,
            'queue_state': queue, 'busy_by': busy_by, 'busy_task': busy_task, 'queued_tasks': queued,
            'heartbeat': heartbeat, **(LOCAL_SCOPE if transport == 'local' else {})}


def inventory_assets(db, users=None):
    local = db.execute('SELECT asset_id,enabled FROM local_host WHERE id=1').fetchone()
    out = []
    for r in db.execute('SELECT * FROM assets WHERE retired_at IS NULL ORDER BY id'):
        if local and r['id'] == local['asset_id'] and not local['enabled']:
            continue
        available = json.loads(r['allowed_users'])
        if users is not None:
            available = [u for u in available if u in users]
            if not available:
                continue
        # User notes share the asset source and model-context redaction policy.
        # Never include stored credential refs, tokens, or host keys.
        out.append({'id': r['id'], 'name': scrubbing.redact_text(r['name']),
                    'notes': scrubbing.redact_text(r['notes']),
                    'connection_type': r['connection_type'], 'available_selected_users': available,
                    **(LOCAL_SCOPE if r['connection_type'] == 'local' else {})})
    return out


def inventory_document(db, users=None):
    assets = inventory_assets(db, users)
    return {'id': INVENTORY_ID, 'name': '注册资产清单（系统实时派生）', 'core': True,
            'read_only': True, 'derived': True, 'assets': assets,
            'content': json.dumps(assets, ensure_ascii=False)}


def install_observability(app, transaction, admin):
    @app.get('/api/v1/assets/{asset_id}/heartbeats', dependencies=[Depends(admin)])
    def samples(asset_id: str, after: int = 0, limit: int = 100, view: str = 'windows'):
        if after < 0 or after > heartbeat_telemetry.MAX_SEQ or not 1 <= limit <= 500 or view not in ('windows', 'attempts'):
            raise HTTPException(422, 'Invalid pagination')
        with transaction() as db:
            if not db.execute('SELECT 1 FROM assets WHERE id=?', (asset_id,)).fetchone():
                raise HTTPException(404, 'Asset not found')
            now = time.time()
            heartbeat_telemetry.prune(db, now)
            rows = heartbeat_telemetry.logs(db, asset_id, after, limit, now) if view == 'attempts' else [dict(r) for r in db.execute('SELECT * FROM heartbeat_samples WHERE asset_id=? AND seq>? AND received_at>=? ORDER BY seq LIMIT ?', (asset_id, after, now-RETENTION_SECONDS, limit))]
            summary = heartbeat_telemetry.view(db, asset_id, now)
        return {'items': rows, 'next_after': rows[-1]['seq'] if rows else after, 'retention_hours': 24,
                'view': view, 'summary': summary}

    @app.get('/api/v1/documents/system-asset-inventory', dependencies=[Depends(admin)])
    def inventory(role_id: str | None = None, execution_users: str | None = None):
        with transaction() as db:
            if role_id and not db.execute('SELECT 1 FROM roles WHERE id=?', (role_id,)).fetchone():
                raise HTTPException(404, 'Role not found')
            return inventory_document(db, execution_users.split(',') if execution_users is not None else None)
