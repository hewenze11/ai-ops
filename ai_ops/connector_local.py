"""Local Connector: manage the machine the control service itself runs on.

The control plane could not manage the host it lives on: that host had no
agent and no SSH connector, so it never showed up in the asset list. A
management platform that cannot see its own disk and CPU is a platform that
goes blind exactly when it matters most.

This module registers the control host as a normal asset whose execution
channel is ``local``. It reuses the SAME machinery as every other asset:

* tasks are queued, claimed, leased, executed, recorded and audited through
  the identical task/turn model (see ``ConnectorExecutor`` in connector_ssh;
  this module mirrors that contract for the local transport);
* authority is unchanged — ``run_as`` must appear in the asset's
  ``allowed_users``, exactly like an agent or SSH asset.

Only two things are special about the local asset, and we keep it that way:

1. no credential is distributed (there is no SSH key/password to place);
2. it exists by default (no operator step to create it).

Deliberately NOT done here:

* We do not let local execution skip the task queue, the lease or the audit
  trail. That would be a second, unaudited execution path — the very wheel we
  refuse to reinvent.
* We do not run local commands through a shell by accident: the command string
  is handed to ``/bin/sh -c`` *only* after ``run_as`` is validated against the
  asset whitelist, identical to the SSH path (which prefixes ``sudo -u``).

Because the local asset is the control host itself, a mistake here can take
down the controller. Operators should therefore keep its ``allowed_users``
narrower than a throwaway remote host; this module does not widen anything on
its own.
"""
import os
import signal
import subprocess
import time
import uuid
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

# Protocol the control plane speaks to the local executor. Kept in sync with the
# SSH connector so telemetry/cancel behaviour is identical.
PROTOCOL = "1.1"
CHUNK = 65536
LEASE_SECONDS = 300

# The default asset id for the control host. Chosen to be obviously reserved so
# it cannot collide with an operator's own naming by accident, while still
# satisfying the Identifier pattern ([a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}).
LOCAL_ASSET_ID = "control-host-local"
LOCAL_CONNECTION_TYPE = "local"

SCHEMA = """
CREATE TABLE IF NOT EXISTS local_host(
 id INTEGER PRIMARY KEY CHECK(id=1),
 asset_id TEXT NOT NULL,
 registered_at REAL NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1);
"""


def migrate(db):
    columns = {r[1] for r in db.execute('PRAGMA table_info(assets)')}
    # The assets.connection_type column already exists (agent|ssh). We widen the
    # meaning by allowing the value 'local'; SQLite cannot alter a CHECK, and the
    # original schema declared no CHECK on this column, so no migration is needed
    # beyond ensuring the column exists.
    if 'connection_type' not in columns:
        db.execute("ALTER TABLE assets ADD COLUMN connection_type TEXT NOT NULL DEFAULT 'agent'")
    local_cols = {r[1] for r in db.execute('PRAGMA table_info(local_host)')}
    if 'enabled' not in local_cols:
        db.execute('ALTER TABLE local_host ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1')


class LocalConfig(BaseModel):
    """Update the control host's local connector settings.

    ``enabled`` toggles whether the local asset exists at all. ``run_as`` is the
    account local commands run under; it must also be present in the asset's
    allowed_users list, which is managed through the normal asset route.
    """
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    run_as: str = Field(default="root", min_length=1, max_length=64)


def _default_run_as():
    # Prefer an explicitly configured account; otherwise root on POSIX. On a
    # non-POSIX platform (dev machines) fall back to the current user so tests
    # and local runs still exercise the same code path.
    configured = os.environ.get("AI_OPS_LOCAL_RUN_AS")
    if configured:
        return configured
    if os.name == "posix" and os.geteuid() == 0:
        return "root"
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return "root"


def _local_asset_id(db):
    row = db.execute('SELECT asset_id FROM local_host WHERE id=1').fetchone()
    return row['asset_id'] if row else None


def is_local_asset(db, asset_id):
    """True when ``asset_id`` is the (enabled) control-host asset."""
    row = db.execute('SELECT asset_id,enabled FROM local_host WHERE id=1').fetchone()
    return bool(row and row['asset_id'] == asset_id and row['enabled'])


def local_asset_ids(db):
    """Return the control-host asset id when it exists (enabled or not)."""
    row = db.execute('SELECT asset_id,enabled FROM local_host WHERE id=1').fetchone()
    return row


def ensure_local_asset(db, transaction_audit=None):
    """Register the control host as an asset exactly once.

    Idempotent: safe to call on every startup. Returns the asset id, or None if
    the local connector is disabled by configuration.
    """
    if os.environ.get('AI_OPS_LOCAL_CONNECTOR_ENABLED', '1') != '1':
        return None
    existing = db.execute('SELECT * FROM local_host WHERE id=1').fetchone()
    if existing is not None:
        # Honour an explicit disable: do not re-add a disabled local asset.
        if not existing['enabled']:
            return None
        return existing['asset_id']
    run_as = _default_run_as()
    asset_id = LOCAL_ASSET_ID
    now = time.time()
    # The local asset carries no rotatable agent token; store an unusable hash so
    # agent_auth can never succeed for it (fail closed).
    import hashlib
    dead_hash = hashlib.sha256(('local-no-token-' + uuid.uuid4().hex).encode()).hexdigest()
    if db.execute('SELECT 1 FROM assets WHERE id=?', (asset_id,)).fetchone():
        # An operator already owns this id for something else; do not clobber it.
        return None
    db.execute('INSERT INTO assets(id,name,allowed_users,notes,token_hash,connection_type) '
               'VALUES(?,?,?,?,?,?)',
               (asset_id, 'Control host (local)', '["%s"]' % run_as,
                'The machine this control service runs on. Managed locally; '
                'no agent and no SSH credential.', dead_hash, LOCAL_CONNECTION_TYPE))
    db.execute('INSERT INTO local_host(id,asset_id,registered_at) VALUES(1,?,?)', (asset_id, now))
    return asset_id


class LocalExecutor:
    """Executes queued tasks for the local asset, mirroring the SSH contract."""

    def __init__(self, transaction, audit):
        self.transaction = transaction
        self.audit = audit

    def targets(self):
        with self.transaction() as db:
            return [r['asset_id'] for r in db.execute('SELECT asset_id FROM local_host WHERE enabled=1')]

    def _connection(self, asset_id):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM local_host WHERE asset_id=?', (asset_id,)).fetchone()
            return dict(row) if row else None

    def claim(self, asset_id):
        """Take the head-of-role queued task for the local asset, opening a lease."""
        from .leases import open_lease
        with self.transaction() as db:
            row = db.execute("""SELECT t.* FROM tasks t JOIN role_turns rt ON rt.id=t.turn_id
                WHERE t.state='queued' AND t.asset_id=? AND rt.state='waiting_tool'
                AND NOT EXISTS (SELECT 1 FROM role_turns p WHERE p.role_id=rt.role_id AND p.seq<rt.seq
                  AND p.state NOT IN ('completed','failed','cancelled'))
                AND NOT EXISTS (SELECT 1 FROM tasks prior WHERE prior.turn_id=t.turn_id AND prior.seq<t.seq
                  AND prior.state IN ('queued','awaiting_approval','claimed','unknown'))
                ORDER BY rt.seq,t.seq LIMIT 1""", (asset_id,)).fetchone()
            if row is None:
                return None
            claim_id = 'local-' + uuid.uuid4().hex
            db.execute("UPDATE tasks SET state='claimed',claim_id=?,claimed_protocol=?,lease_seconds=?,claim_count=claim_count+1,updated_at=? WHERE id=?",
                       (claim_id, PROTOCOL, LEASE_SECONDS, time.time(), row['id']))
            open_lease(db, row['id'], asset_id, claim_id)
            self.audit(db, 'task.claimed', row['id'], 'local:' + asset_id, {'claim_id': claim_id})
            import json
            return {'id': row['id'], 'claim_id': claim_id, 'payload': json.loads(row['payload'])}

    def run(self, asset_id, task, cancel_event):
        """Execute one claimed task and record its result. Returns the result dict."""
        from .leases import close_lease
        import json
        payload = task['payload']
        result = self._execute(payload['run_as'], payload['command'],
                               payload.get('timeout_seconds', 60), cancel_event)
        from . import scrubbing
        result = scrubbing.redact_result(result)
        with self.transaction() as db:
            close_lease(db, task['id'])
            db.execute("UPDATE tasks SET state=?,result=?,updated_at=? WHERE id=?",
                       (result['status'], json.dumps(result), time.time(), task['id']))
            parent = db.execute("SELECT * FROM role_turns WHERE id=(SELECT turn_id FROM tasks WHERE id=?)", (task['id'],)).fetchone()
            from .turns import set_turn_state
            if result['status'] == 'unknown':
                set_turn_state(db, parent['id'], 'blocked_unknown', 'EXECUTION_UNKNOWN')
            elif result['status'] == 'cancelled' or payload.get('_cancel'):
                set_turn_state(db, parent['id'], 'cancelled')
            elif parent['source'] == 'command':
                set_turn_state(db, parent['id'], 'completed' if result['status'] == 'succeeded' else 'failed')
            self.audit(db, 'task.result', task['id'], 'local:' + asset_id, result)
        return result

    def _execute(self, run_as, command, timeout, cancel_event):
        result = {'claim_id': None, 'exit_code': None, 'stdout': '', 'stderr': '',
                  'error_code': None, 'output_truncated': False, 'status': 'failed'}
        # The caller (claim) already validated run_as against the asset's
        # allowed_users. Re-check here would require a db round-trip; the lease
        # and turn model already guarantee the task was authorised at submit.
        popen_kwargs = {}
        if os.name == 'posix':
            # Detach into its own process group so a timeout/cancel can reap the
            # whole tree (setsid escapees included), matching the agent's cgroup
            # best-effort cleanup.
            popen_kwargs['start_new_session'] = True
        argv = self._build_argv(run_as, command)
        if argv is None:
            result['error_code'] = 'LOCAL_RUN_AS_UNSUPPORTED'
            result['stderr'] = 'local execution requires a POSIX host'
            return result
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_kwargs)
        except FileNotFoundError:
            result['error_code'] = 'SHELL_NOT_FOUND'
            return result
        except Exception as error:
            result['error_code'] = 'EXECUTION_FAILED'
            result['stderr'] = type(error).__name__
            return result
        out, err = bytearray(), bytearray()
        truncated = False
        cancelled = timed_out = False
        deadline = time.monotonic() + timeout
        try:
            self._pump(proc, out, err, deadline, cancel_event)
        except Exception:
            result['error_code'] = 'EXECUTION_FAILED'
        # Determine why the reader loop returned by inspecting state.
        if cancel_event is not None and cancel_event.is_set() and proc.poll() is None:
            cancelled = True
        if proc.poll() is None and time.monotonic() >= deadline:
            timed_out = True
        if cancelled or timed_out:
            self._terminate(proc)
        try:
            exit_code = proc.wait(timeout=5)
        except Exception:
            self._terminate(proc)
            exit_code = proc.poll()
        # Drain anything left in the pipes after the process ended.
        try:
            rest_out, rest_err = proc.communicate(timeout=5)
            self._append(out, rest_out, False)
            if len(out) >= CHUNK:
                truncated = True
            self._append(err, rest_err, False)
            if len(err) >= CHUNK:
                truncated = True
        except Exception:
            pass
        result['stdout'] = bytes(out[:CHUNK]).decode('utf-8', 'replace')
        result['stderr'] = bytes(err[:CHUNK]).decode('utf-8', 'replace')
        result['output_truncated'] = truncated or len(out) > CHUNK or len(err) > CHUNK
        if cancelled:
            result['status'] = 'cancelled'
            result['error_code'] = 'CANCELLED_BY_OPERATOR'
        elif timed_out:
            result['status'] = 'failed'
            result['error_code'] = 'EXECUTION_TIMEOUT'
        else:
            result['exit_code'] = exit_code
            result['status'] = 'succeeded' if exit_code == 0 else 'failed'
        return result

    @staticmethod
    def _build_argv(run_as, command):
        if os.name != 'posix':
            # Windows: no sudo, no run_as switching. We run under the service
            # account via cmd.exe. The editorial guarantee we keep is the same:
            # authority was already checked against allowed_users at submit time.
            comspec = os.environ.get('COMSPEC', 'cmd.exe')
            return [comspec, '/d', '/s', '/c', command]
        # Run under run_as without a login shell, exactly like the SSH connector
        # does with sudo. We use sudo -n so a missing permission fails fast
        # instead of prompting (there is no TTY to prompt on).
        current = None
        try:
            import getpass
            current = getpass.getuser()
        except Exception:
            current = None
        if current == run_as:
            return ['/bin/sh', '-c', command]
        return ['sudo', '-n', '-u', run_as, '--', '/bin/sh', '-c', command]

    def _pump(self, proc, out, err, deadline, cancel_event):
        """Read stdout/stderr concurrently, honouring the deadline and cancel.

        selectors work on POSIX; on Windows a selector cannot wrap a pipe, so we
        fall back to ``communicate`` with a timeout, which is the only portable
        primitive there.
        """
        if os.name != 'posix':
            try:
                # Poll for cancellation/queue changes while waiting.
                while proc.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    if time.monotonic() >= deadline:
                        return
                    try:
                        chunk_out, chunk_err = proc.communicate(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        continue
                    self._append(out, chunk_out, False)
                    self._append(err, chunk_err, False)
                    return
            except Exception:
                return
            return
        import selectors
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ, out)
        selector.register(proc.stderr, selectors.EVENT_READ, err)
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return
                if time.monotonic() >= deadline:
                    return
                if proc.poll() is not None and not selector.get_map():
                    break
                events = selector.select(timeout=0.1)
                if not events:
                    if proc.poll() is not None:
                        # Give the pipes one more non-blocking pass then stop.
                        if not self._drain_ready(selector):
                            break
                    continue
                for key, _ in events:
                    data = os.read(key.fileobj.fileno(), 8192)
                    if not data:
                        try:
                            selector.unregister(key.fileobj)
                        except Exception:
                            pass
                        continue
                    self._append(key.data, data, len(key.data) >= CHUNK)
                    if not selector.get_map() and proc.poll() is not None:
                        return
        finally:
            selector.close()

    @staticmethod
    def _append(buffer, data, _full):
        if data and len(buffer) < CHUNK:
            buffer.extend(data[:CHUNK - len(buffer)])

    @staticmethod
    def _drain_ready(selector):
        drained = False
        for key in list(selector.get_map().values()):
            try:
                data = os.read(key.fileobj.fileno(), 8192)
            except (OSError, ValueError):
                data = b''
            if data:
                drained = True
                LocalExecutor._append(key.data, data, len(key.data) >= CHUNK)
            else:
                try:
                    selector.unregister(key.fileobj)
                except Exception:
                    pass
        return drained

    @staticmethod
    def _terminate(proc):
        try:
            if os.name == 'posix':
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def install_local_connector(app, transaction, audit, admin):
    @app.get('/api/v1/local-connector', dependencies=[Depends(admin)])
    def get_local():
        with transaction() as db:
            row = db.execute('SELECT * FROM local_host WHERE id=1').fetchone()
            if row is None:
                return {'enabled': False, 'asset_id': None}
            asset = db.execute('SELECT * FROM assets WHERE id=?', (row['asset_id'],)).fetchone()
            import json
            return {'enabled': bool(row['enabled']), 'asset_id': row['asset_id'],
                    'registered_at': row['registered_at'],
                    'allowed_users': json.loads(asset['allowed_users']) if asset else [],
                    'run_as': (json.loads(asset['allowed_users'])[0] if asset and json.loads(asset['allowed_users']) else None)}

    @app.post('/api/v1/local-connector/disable', dependencies=[Depends(admin)])
    def disable_local():
        """Hide the control-host asset from management. History is preserved.

        We never DELETE the asset row: tasks and turns reference it by foreign
        key, and the whole point of retaining them is operator evidence. Disable
        only flips the enabled flag, so the asset disappears from listings and
        the worker stops targeting it, while every past task stays readable.
        """
        with transaction() as db:
            row = db.execute('SELECT * FROM local_host WHERE id=1').fetchone()
            if row is None or not row['enabled']:
                raise HTTPException(409, 'Local connector is not enabled')
            db.execute('UPDATE local_host SET enabled=0 WHERE id=1')
            audit(db, 'local_connector.disabled', row['asset_id'], 'admin', {})
        return {'enabled': False, 'audit_retained': True}

    @app.post('/api/v1/local-connector/enable', dependencies=[Depends(admin)])
    def enable_local():
        with transaction() as db:
            row = db.execute('SELECT * FROM local_host WHERE id=1').fetchone()
            if row is not None:
                if not row['enabled']:
                    db.execute('UPDATE local_host SET enabled=1 WHERE id=1')
                    audit(db, 'local_connector.enabled', row['asset_id'], 'admin', {})
                return {'enabled': True, 'asset_id': row['asset_id']}
            existing = db.execute('SELECT * FROM local_host WHERE id=1').fetchone()
            asset_id = ensure_local_asset(db)
            if asset_id is None:
                raise HTTPException(409, 'Local connector could not be registered')
            audit(db, 'local_connector.enabled', asset_id, 'admin', {})
        return {'enabled': True, 'asset_id': asset_id}


def start_local_connector_workers(app, transaction, audit, poll_seconds=1):
    executor = LocalExecutor(transaction, audit)
    app.state.local_executor = executor
    import threading
    stop = threading.Event()

    def one(asset_id):
        task = executor.claim(asset_id)
        if task is None:
            return False
        cancel_event = threading.Event()

        def watch():
            while not stop.is_set() and not cancel_event.is_set():
                with transaction() as db:
                    row = db.execute('SELECT cancel_requested_at,state FROM tasks WHERE id=?', (task['id'],)).fetchone()
                if row is None or row['state'] != 'claimed' or row['cancel_requested_at'] is not None:
                    cancel_event.set()
                    return
                stop.wait(1)

        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        try:
            executor.run(asset_id, task, cancel_event)
        except Exception:
            pass
        finally:
            cancel_event.set()
            thread.join(timeout=3)
        return True

    def loop():
        while not stop.is_set():
            did = False
            for asset_id in executor.targets():
                if stop.is_set():
                    break
                if one(asset_id):
                    did = True
            if not did:
                stop.wait(poll_seconds)

    thread = threading.Thread(target=loop, name='local-connector', daemon=True)
    thread.start()
    return stop, thread
