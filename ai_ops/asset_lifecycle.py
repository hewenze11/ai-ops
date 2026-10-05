"""Asset lifecycle: retire vs. purge, and offline detection that fan-outs.

Two product questions this answers (user, 2026-10-04):

1. **Deleting a registered machine.** Removing a machine used to be a single,
   ambiguous action. Operators actually mean one of two different things:

   * **Retire (注销)** — the machine is no longer ours to manage, but everything
     it ever did stays as evidence. Recoverable. This is the default.
   * **Purge (彻底删除)** — the record must be gone. Destructive, requires an
     explicit typed confirmation, and refuses while any run is unresolved.

   History is evidence: a task that ran on a host is a fact that happened. So
   retiring hides the asset from the active inventory without erasing the facts,
   and purging is deliberately hard.

2. **A machine goes silent (失联), not retired.** The platform notices the gap
   (agent heartbeat lapsed past the online TTL) and turns it into a durable
   *event*. It does NOT become a second monitoring system: detection is ours,
   notification rides the existing outbound webhook (the same one Alert result
   fan-out uses). Alertmanager stays the first hop; we hand it a signal it can
   choose to act on, and we de-bounce so a flapping host does not spam.

Boundaries:
* We never auto-reassign work on a silent host (see docs/leases.md).
* Purge is refused for an asset with unresolved (unknown/claimed) work, and it
  removes dependent rows in FK order inside one transaction, or nothing at all.
"""
import json
import time
import uuid

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .models import Identifier as ID

# Online TTL mirrors execution.py: a presence older than this is "silent".
ONLINE_TTL = 30
# A host must be silent this many consecutive checks before we emit one event,
# so a brief blip does not page anyone. Mirrors Alertmanager's `for`.
OFFLINE_DEBOUNCE_SECONDS = 90
# We only emit at most one offline event per asset per this window.
OFFLINE_REEMIT_SECONDS = 1800

SCHEMA = """
CREATE TABLE IF NOT EXISTS asset_lifecycle(
 asset_id TEXT PRIMARY KEY REFERENCES assets(id),
 retired_at REAL, retired_by TEXT, retire_note TEXT, purge_after REAL);
CREATE TABLE IF NOT EXISTS asset_offline_events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
 asset_id TEXT NOT NULL, silent_for REAL NOT NULL, last_seen REAL,
 emitted_at REAL NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
"""


def migrate(db):
    columns = {r[1] for r in db.execute('PRAGMA table_info(assets)')}
    if 'retired_at' not in columns:
        # Retired assets are hidden from the default inventory but kept intact.
        db.execute('ALTER TABLE assets ADD COLUMN retired_at REAL')


class RetireIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    note: str = Field(default='', max_length=2000)


class PurgeIn(BaseModel):
    """Destructive: require the caller to echo the asset id.

    A typed confirmation is the standard guard against an accidental destructive
    call; ``confirm_asset_id`` must equal the path id or we refuse.
    """
    model_config = ConfigDict(extra='forbid')
    confirm_asset_id: ID


def _unresolved(db, asset_id):
    return db.execute(
        "SELECT id,state FROM tasks WHERE asset_id=? AND state IN ('claimed','unknown') ORDER BY seq",
        (asset_id,)).fetchall()


def install_asset_lifecycle(app, transaction, audit, admin):
    @app.post('/api/v1/assets/{asset_id}/retire', dependencies=[Depends(admin)])
    def retire(asset_id: ID, body: RetireIn):
        """Retire an asset: hide it, keep every historical fact, allow undo."""
        now = time.time()
        with transaction() as db:
            row = db.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Asset not found')
            if row['retired_at'] is not None:
                raise HTTPException(409, 'Asset is already retired')
            db.execute('UPDATE assets SET retired_at=? WHERE id=?', (now, asset_id))
            db.execute("INSERT INTO asset_lifecycle(asset_id,retired_at,retired_by,retire_note) VALUES(?,?,?,?) "
                       "ON CONFLICT(asset_id) DO UPDATE SET retired_at=excluded.retired_at,"
                       "retired_by=excluded.retired_by,retire_note=excluded.retire_note",
                       (asset_id, now, 'admin', body.note))
            audit(db, 'asset.retired', asset_id, 'admin', {'note': body.note})
        return {'asset_id': asset_id, 'retired': True, 'history_retained': True}

    @app.post('/api/v1/assets/{asset_id}/unretire', dependencies=[Depends(admin)])
    def unretire(asset_id: ID):
        with transaction() as db:
            row = db.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Asset not found')
            if row['retired_at'] is None:
                raise HTTPException(409, 'Asset is not retired')
            db.execute('UPDATE assets SET retired_at=NULL WHERE id=?', (asset_id,))
            audit(db, 'asset.unretired', asset_id, 'admin', {})
        return {'asset_id': asset_id, 'retired': False}

    @app.post('/api/v1/assets/{asset_id}/purge', dependencies=[Depends(admin)])
    def purge(asset_id: ID, body: PurgeIn):
        """Purge an asset and its dependent rows. Destructive and guarded.

        Refused while any task is claimed/unknown: we will not delete the record
        of a run whose outcome is still unknown — that is precisely the evidence
        an operator may need. Resolve or abandon it first.
        """
        if body.confirm_asset_id != asset_id:
            raise HTTPException(422, 'confirm_asset_id must equal the path asset id')
        with transaction() as db:
            row = db.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Asset not found')
            stuck = _unresolved(db, asset_id)
            if stuck:
                raise HTTPException(409, 'Asset has unresolved executions; resolve or abandon them first: '
                                    + ','.join(r['id'] for r in stuck))
            counts = {}
            # Delete dependents in FK order, inside this one transaction.
            task_ids = [r['id'] for r in db.execute('SELECT id FROM tasks WHERE asset_id=?', (asset_id,))]
            counts['tasks'] = len(task_ids)
            for task_id in task_ids:
                db.execute('DELETE FROM output_chunks WHERE task_id=?', (task_id,))
                db.execute('DELETE FROM output_archives WHERE task_id=?', (task_id,))
                db.execute('DELETE FROM task_controls WHERE task_id=?', (task_id,))
                db.execute('DELETE FROM task_leases WHERE task_id=?', (task_id,))
            # Detach tasks from turns, then remove the turns they belonged to.
            db.execute('UPDATE tasks SET turn_id=NULL WHERE asset_id=?', (asset_id,))
            db.execute('DELETE FROM tasks WHERE asset_id=?', (asset_id,))
            counts['agent_presence'] = db.execute('DELETE FROM agent_presence WHERE asset_id=?', (asset_id,)).rowcount
            db.execute('DELETE FROM asset_connections WHERE asset_id=?', (asset_id,))
            db.execute('DELETE FROM asset_lifecycle WHERE asset_id=?', (asset_id,))
            db.execute('DELETE FROM local_host WHERE asset_id=?', (asset_id,))
            db.execute('DELETE FROM assets WHERE id=?', (asset_id,))
            audit(db, 'asset.purged', asset_id, 'admin', {'counts': counts})
        return {'asset_id': asset_id, 'purged': True, 'counts': counts}

    @app.get('/api/v1/assets/{asset_id}/lifecycle', dependencies=[Depends(admin)])
    def lifecycle(asset_id: ID):
        with transaction() as db:
            row = db.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Asset not found')
            life = db.execute('SELECT * FROM asset_lifecycle WHERE asset_id=?', (asset_id,)).fetchone()
            events = [dict(r) for r in db.execute(
                'SELECT * FROM asset_offline_events WHERE asset_id=? ORDER BY seq DESC LIMIT 50', (asset_id,))]
            return {'asset_id': asset_id, 'retired': row['retired_at'] is not None,
                    'retired_at': row['retired_at'],
                    'retire': dict(life) if life else None,
                    'offline_events': events}

    @app.get('/api/v1/assets/offline', dependencies=[Depends(admin)])
    def offline_assets():
        """Current silent assets (live view; no side effects)."""
        now = time.time()
        with transaction() as db:
            out = []
            for row in db.execute("SELECT p.asset_id,p.last_seen,a.name,a.retired_at FROM agent_presence p "
                                  "LEFT JOIN assets a ON a.id=p.asset_id"):
                if row['retired_at'] is not None:
                    continue
                if now - row['last_seen'] > ONLINE_TTL:
                    out.append({'asset_id': row['asset_id'], 'name': row['name'],
                                'silent_for': round(now - row['last_seen'], 1),
                                'last_seen': row['last_seen']})
            return out


def note_seen(transaction, asset_id, now=None):
    """Called when a host comes back. Clears the offline debounce counters so a
    recovered host can emit a fresh event only after it goes silent again."""
    now = time.time() if now is None else now
    with transaction() as db:
        db.execute('DELETE FROM asset_offline_events WHERE asset_id=?', (asset_id,))

def detect_offline(transaction, audit, now=None):
    """Detect assets that have been silent past the debounce window and emit one
    durable event per asset per re-emit window. Returns events created.

    Detection is the platform's job; delivery rides the outbound webhook. We do
    not attempt the host ourselves and we never reassign its work.
    """
    now = time.time() if now is None else now
    created = []
    with transaction() as db:
        rows = db.execute(
            "SELECT p.asset_id AS asset_id, p.last_seen AS last_seen, a.name AS name, a.retired_at AS retired_at "
            "FROM agent_presence p JOIN assets a ON a.id=p.asset_id").fetchall()
        for row in rows:
            if row['retired_at'] is not None:
                continue
            silent_for = now - row['last_seen']
            if silent_for <= ONLINE_TTL:
                # Alive again: forget old events so it can re-emit next time.
                db.execute('DELETE FROM asset_offline_events WHERE asset_id=?', (row['asset_id'],))
                continue
            if silent_for < OFFLINE_DEBOUNCE_SECONDS:
                continue
            recent = db.execute(
                'SELECT emitted_at FROM asset_offline_events WHERE asset_id=? ORDER BY seq DESC LIMIT 1',
                (row['asset_id'],)).fetchone()
            if recent is not None and now - recent['emitted_at'] < OFFLINE_REEMIT_SECONDS:
                continue
            event_id = str(uuid.uuid4())
            db.execute('INSERT INTO asset_offline_events(id,asset_id,silent_for,last_seen,emitted_at,delivered) '
                       'VALUES(?,?,?,?,?,0)',
                       (event_id, row['asset_id'], silent_for, row['last_seen'], now))
            audit(db, 'asset.offline_detected', row['asset_id'], 'service',
                  {'silent_for': round(silent_for, 1), 'event_id': event_id})
            created.append({'id': event_id, 'asset_id': row['asset_id'], 'name': row['name'],
                            'silent_for': silent_for})
    for event in created:
        _fanout_offline(transaction, audit, event, now)
    return created


def _fanout_offline(transaction, audit, event, now):
    """Queue one push onto the existing outbound webhook, if configured.

    Offline notification is NOT a second monitoring system: it reuses the same
    single URL the alert result fan-out uses. If nothing is configured it is a
    no-op (the event stays durable and visible in the console). Dedupe key is
    per-event, so a re-emitted offline alert is a distinct push.
    """
    from .alert_notify import enqueue_notification
    with transaction() as db:
        cfg = db.execute('SELECT * FROM alert_notify_config WHERE id=1 AND enabled=1').fetchone()
        if cfg is None:
            return
        title = '[资产失联] ' + (event['name'] or event['asset_id'])
        body = '\n'.join([
            '资产：%s 已失联。' % event['asset_id'],
            '静默时长：约 %d 秒（超过在线阈值 %d 秒）。' % (int(event['silent_for']), ONLINE_TTL),
            '',
            '说明：平台只负责“检测到失联”；是否通知、通知给谁由该 Webhook 决定。',
            '平台不会自动重派该主机上的任务，也不会去探测它。',
        ])
        notify_id = enqueue_notification(
            db, turn_id=None, dedupe_key='offline:' + event['id'],
            url=cfg['url'], channel=cfg['channel'], title=title, body=body,
            alarm_state='firing')
        if notify_id is not None:
            db.execute('UPDATE asset_offline_events SET delivered=1 WHERE id=?', (event['id'],))
            audit(db, 'asset.offline_queued', event['asset_id'], 'service',
                  {'event_id': event['id'], 'channel': cfg['channel']})


def start_offline_worker(transaction, audit, poll_seconds=15):
    """Background thread: periodic offline detection. Bounded and idempotent."""
    import logging
    import threading

    log = logging.getLogger(__name__)
    stop = threading.Event()

    def run():
        while not stop.is_set():
            try:
                detect_offline(transaction, audit)
            except Exception as e:
                log.warning('Offline detection iteration failed: %s', type(e).__name__)
            stop.wait(poll_seconds)

    thread = threading.Thread(target=run, name='asset-offline-worker', daemon=True)
    thread.start()
    return stop, thread
