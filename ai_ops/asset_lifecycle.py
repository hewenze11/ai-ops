"""Asset lifecycle: retire vs. purge, and offline detection that fan-outs.

Two product questions this answers (user, 2026-10-04):

1. **Deleting a registered machine.** Removing a machine used to be a single,
   ambiguous action. Operators actually mean one of two different things:

   * **Retire (注销)** - the machine is no longer ours to manage, but everything
     it ever did stays as evidence. Recoverable. This is the default.
   * **Purge (彻底删除)** - the record must be gone. Destructive, requires an
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


# Response key overrides. The purge response is read by the console and by tests,
# so keys that existed before the schema-driven rewrite keep their old names.
_COUNT_KEYS = {'asset_account_notes': 'account_notes'}


def _asset_fk_children(db):
    """Every table with a foreign key onto ``assets``, as (table, column) pairs.

    Derived from the live schema instead of a constant list: a hand-maintained
    list is exactly what made purge fail once tables gained an ``asset_id`` and
    nobody updated this module.
    """
    rows = db.execute(
        "SELECT m.name AS child, p.'from' AS col "
        "FROM sqlite_master AS m, pragma_foreign_key_list(m.name) AS p "
        "WHERE m.type='table' AND p.'table'='assets' "
        "ORDER BY m.name"
    ).fetchall()
    return [(r['child'], r['col']) for r in rows]


def _tables_with_asset_column(db):
    """Every table that carries an ``asset_id`` column, whether or not it
    declares a foreign key.

    Foreign keys are not the whole story. Nine tables hold an `asset_id` with no
    FK declared (`asset_offline_events`, `credential_events`, `wait_handles`,
    `fanout_children`, `task_leases`, `blocked_kinds`, `local_host`,
    `sensitive_hits`, `account_requests`). Purging only what the FK graph
    mentions leaves those rows behind, pointing at an asset that no longer
    exists -- the same class of mess the FK bug produced, just quieter because
    nothing raises.
    """
    names = [r['name'] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    found = []
    for name in names:
        if name == 'assets':
            continue
        try:
            columns = [c[1] for c in db.execute('PRAGMA table_info("%s")' % name)]
        except Exception:
            continue
        if 'asset_id' in columns:
            found.append(name)
    return found


def _dependent_chain(db, roots):
    """Every table whose rows belong to an asset, children-first.

    Two layers, because one is not enough:

    * ``roots`` — tables that carry an ``asset_id`` column. These are deleted
      with ``WHERE asset_id = ?``. This covers tables that declare no foreign key
      at all (asset_offline_events, credential_events, wait_handles,
      fanout_children, blocked_kinds, local_host, ...). Purging only whatever the
      FK graph mentions leaves those rows behind, pointing at an asset that no
      longer exists.
    * grandchildren — tables with no ``asset_id`` that are reached through a
      foreign key from some other asset-scoped table (``heartbeat_attempts``
      through ``heartbeat_streams``). These are deleted through a rowid subquery
      on their parent.

    Ordering used to be a hand-written line for the single grandchild that
    existed. That is the same shape of bug this module was already fixed for:
    add another grandchild and the delete fails with a foreign key error *after*
    several tables have been emptied. Deriving the order means that cannot
    happen again.

    Returns [(table, sql, uses_asset_param)] ordered so that a table is always
    deleted before the table it points at.
    """
    names = [r['name'] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    columns = {}
    for name in names:
        columns[name] = [c[1] for c in db.execute('PRAGMA table_info("%s")' % name)]

    # edge: child table -> (column, parent table)
    edges = {}
    for row in db.execute(
            "SELECT m.name AS child, p.'from' AS col, p.'table' AS parent "
            "FROM sqlite_master AS m, pragma_foreign_key_list(m.name) AS p "
            "WHERE m.type='table'"):
        if row['parent'] in columns:
            edges.setdefault(row['child'], []).append((row['col'], row['parent']))

    root_set = {t for t in roots if 'asset_id' in columns.get(t, [])}

    # Walk down from the roots to find grandchildren and their depth.
    depth = {t: 0 for t in root_set}
    frontier = sorted(root_set)
    while frontier:
        nxt = []
        for parent in frontier:
            for child, parent_edges in edges.items():
                for col, target in parent_edges:
                    if target != parent or child in root_set or child in depth:
                        continue
                    depth[child] = depth[parent] + 1
                    nxt.append(child)
        frontier = sorted(set(nxt))

    plan = []
    for table in sorted(depth, key=lambda t: (-depth[t], t)):
        if table in root_set:
            plan.append((table, 'DELETE FROM "%s" WHERE "asset_id"=?' % table, True))
            continue
        # Grandchild: exactly one FK pointing at a table we are also deleting.
        col, parent = edges[table][0]
        plan.append((
            table,
            'DELETE FROM "%s" WHERE "%s" IN (SELECT rowid FROM "%s" WHERE asset_id=?)'
            % (table, col, parent) if parent in root_set else
            'DELETE FROM "%s" WHERE "%s" IN '
            '(SELECT rowid FROM "%s" WHERE "asset_id" IN '
            '(SELECT "asset_id" FROM "%s" WHERE "asset_id"=?))'
            % (table, col, parent, parent),
            True))
    return plan


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
        of a run whose outcome is still unknown - that is precisely the evidence
        an operator may need. Resolve or abandon it first.

        Deletion order is derived from the schema, not hand-listed. The previous
        hand-written list silently missed tables that also hold a foreign key to
        `assets` (host_tags, credential_handles, sensitive_hits,
        account_requests), so the final `DELETE FROM assets` raised a
        foreign-key error and the transaction rolled back *after* several child
        tables had already been emptied -- leaving an asset that could not be
        removed plus rows pointing at nothing. Reading the schema means a table
        added later is handled without anyone remembering to update this
        function.
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
            # Delete dependents children-first. The order is derived from the
            # schema (see _dependent_chain), not hand-listed: hand lists are
            # what missed host_tags / credential_handles / sensitive_hits /
            # account_requests, and what would miss any future grandchild.
            roots = _tables_with_asset_column(db)
            plan = _dependent_chain(db, roots)
            # tasks own rows keyed by task_id that carry no asset_id; they are
            # listed here because the generic plan only reaches tables that have
            # an asset column or an FK onto one.
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
            for table, sql, _uses_asset in plan:
                if table == 'tasks':
                    continue  # already deleted above, together with its own children
                result = db.execute(sql, (asset_id,))
                if result.rowcount:
                    # `counts` keys are part of the response contract, so tables
                    # whose key predates this rewrite keep their old name.
                    counts[_COUNT_KEYS.get(table, table)] = result.rowcount
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
            '资产:%s 已失联。' % event['asset_id'],
            '静默时长:约 %d 秒(超过在线阈值 %d 秒)。' % (int(event['silent_for']), ONLINE_TTL),
            '',
            '说明:平台只负责"检测到失联";是否通知、通知给谁由该 Webhook 决定。',
            '平台不会自动重派该主机上的任务,也不会去探测它。',
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
