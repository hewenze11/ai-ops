"""SSH Connector: execute tasks on assets that are reached directly over SSH.

An asset chooses exactly one connection type at registration (agent or ssh).
For ssh assets the server itself connects and runs the command; there is no
agent on the target. This module keeps the connection metadata out of the
assets row and the secret out of every response, audit record and log.
"""
import base64
import hashlib
import io
import json
import os
import threading
import time
import uuid
from typing import Literal

import paramiko
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

PROTOCOL = "1.1"
CHUNK = 65536
ARCHIVE_LIMIT = 64 * 1024 * 1024
CONNECT_TIMEOUT = 15
LEASE_SECONDS = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS asset_connections(
 asset_id TEXT PRIMARY KEY REFERENCES assets(id),
 connection_type TEXT NOT NULL DEFAULT 'agent',
 ssh_host TEXT, ssh_port INTEGER NOT NULL DEFAULT 22, ssh_user TEXT,
 ssh_auth_kind TEXT, ssh_secret_ref TEXT, ssh_host_key TEXT);
"""


def migrate(db):
    columns = {r[1] for r in db.execute('PRAGMA table_info(assets)')}
    # Older databases only ever had agent assets; the row default covers them.
    if 'connection_type' not in columns:
        db.execute("ALTER TABLE assets ADD COLUMN connection_type TEXT NOT NULL DEFAULT 'agent'")
    conn_cols = {r[1] for r in db.execute('PRAGMA table_info(asset_connections)')}
    if 'ssh_host_key' not in conn_cols:
        db.execute("ALTER TABLE asset_connections ADD COLUMN ssh_host_key TEXT")


def save_connection(db, asset):
    db.execute(
        "INSERT INTO asset_connections(asset_id,connection_type,ssh_host,ssh_port,ssh_user,ssh_auth_kind,ssh_secret_ref,ssh_host_key) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET connection_type=excluded.connection_type,"
        "ssh_host=excluded.ssh_host,ssh_port=excluded.ssh_port,ssh_user=excluded.ssh_user,"
        "ssh_auth_kind=excluded.ssh_auth_kind,ssh_secret_ref=excluded.ssh_secret_ref,ssh_host_key=excluded.ssh_host_key",
        (asset.id, asset.connection_type, asset.ssh_host, asset.ssh_port, asset.ssh_user,
         asset.ssh_auth_kind, asset.ssh_secret_ref, asset.ssh_host_key))
    db.execute("UPDATE assets SET connection_type=? WHERE id=?", (asset.connection_type, asset.id))


def connection_view(db, asset_id):
    row = db.execute("SELECT * FROM asset_connections WHERE asset_id=?", (asset_id,)).fetchone()
    if row is None:
        legacy = db.execute("SELECT connection_type FROM assets WHERE id=?", (asset_id,)).fetchone()
        return {"connection_type": (legacy["connection_type"] if legacy else "agent")}
    return {"connection_type": row["connection_type"], "ssh_host": row["ssh_host"],
            "ssh_port": row["ssh_port"], "ssh_user": row["ssh_user"], "ssh_auth_kind": row["ssh_auth_kind"],
            "ssh_host_key_pinned": bool(row["ssh_host_key"])}


def _load_secret(secret_ref):
    """Read the SSH secret from a server-side file. Never return it upward.

    The file AND every parent directory must be traversable by the service user
    (uid 10001); otherwise this raises PermissionError, which we surface as a
    distinct error code so operators do not chase a phantom network problem.
    """
    try:
        with open(secret_ref, "rb") as handle:
            return handle.read()
    except PermissionError as error:
        raise paramiko.SSHException(
            "SECRET_NOT_READABLE: %s is not readable by the service user; "
            "the file and all its parent directories must be readable/traversable "
            "(e.g. dir 755, file 600 owned by the container user 10001)" % secret_ref) from error
    except FileNotFoundError as error:
        raise paramiko.SSHException(
            "SECRET_NOT_FOUND: %s does not exist; check ssh_secret_ref and the "
            "connector mount" % secret_ref) from error


def _shell_quote(value):
    return "'" + value.replace("'", "'\\''") + "'"


def _load_pinned_key(host, line):
    """Parse an OpenSSH public-key line into a Key so we can pin it.

    paramiko dropped ``PKey.from_openssh_public_key`` in 5.x, so we round-trip
    through ``HostKeys`` (known_hosts format), which is stable across majors.
    """
    import tempfile
    text = '%s %s\n' % (host, line.strip())
    handle, path = tempfile.mkstemp(prefix='aiops-hk-')
    try:
        os.write(handle, text.encode())
        os.close(handle)
        keys = paramiko.HostKeys(path)
        entry = keys.lookup(host)
        if entry is None:
            raise paramiko.SSHException('pinned SSH host key is not a valid public key')
        for key_type, key in entry.items():
            return key_type, key
        raise paramiko.SSHException('pinned SSH host key is not a valid public key')
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _classify_connect_error(error):
    """Map a connect-time failure to an operator-actionable error code."""
    text = str(error)
    if text.startswith('SECRET_NOT_READABLE'):
        return 'SECRET_NOT_READABLE'
    if text.startswith('SECRET_NOT_FOUND'):
        return 'SECRET_NOT_FOUND'
    if 'SSH host key is not pinned' in text:
        return 'HOST_KEY_NOT_PINNED'
    if 'pinned SSH host key is not a valid public key' in text:
        return 'HOST_KEY_INVALID'
    return 'CONNECTION_FAILED'


def _connect(info):
    if not info.get('ssh_host_key'):
        # Fail closed: without a pinned host key we would be trusting whatever
        # answers, which is exactly the man-in-the-middle we refuse to accept.
        raise paramiko.SSHException('SSH host key is not pinned for this asset')
    secret = _load_secret(info['ssh_secret_ref'])
    key_type, pinned = _load_pinned_key(info['ssh_host'], info['ssh_host_key'])
    client = paramiko.SSHClient()
    # Pin the operator-provided public key and reject anything else.
    client.get_host_keys().add(info['ssh_host'], key_type, pinned)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    kwargs = {'hostname': info['ssh_host'], 'port': info['ssh_port'], 'username': info['ssh_user'],
              'timeout': CONNECT_TIMEOUT, 'banner_timeout': CONNECT_TIMEOUT,
              'auth_timeout': CONNECT_TIMEOUT, 'allow_agent': False, 'look_for_keys': False}
    if info['ssh_auth_kind'] == 'key':
        kwargs['pkey'] = paramiko.Ed25519Key.from_private_key(io.StringIO(secret.decode()))
    else:
        kwargs['password'] = secret.decode()
    client.connect(**kwargs)
    return client


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


def install_connector(app, transaction, audit, admin):
    @app.get('/api/v1/assets/{asset_id}/connection', dependencies=[Depends(admin)])
    def get_connection(asset_id: str):
        with transaction() as db:
            if not db.execute('SELECT 1 FROM assets WHERE id=?', (asset_id,)).fetchone():
                raise HTTPException(404, 'Asset not found')
            # Never include ssh_secret_ref here.
            return connection_view(db, asset_id)

    @app.post('/api/v1/assets/{asset_id}/ssh/check', dependencies=[Depends(admin)])
    def check_ssh(asset_id: str):
        with transaction() as db:
            row = db.execute('SELECT * FROM asset_connections WHERE asset_id=?', (asset_id,)).fetchone()
            if row is None or row['connection_type'] != 'ssh':
                raise HTTPException(409, 'Asset is not an SSH connector')
            info = dict(row)
        try:
            client = _connect(info)
            transport = client.get_transport()
            reachable = transport is not None and transport.is_active()
            client.close()
        except Exception as error:
            with transaction() as db:
                audit(db, 'ssh.check_failed', asset_id, 'admin', {'error_type': type(error).__name__})
            return {'reachable': False, 'error_type': type(error).__name__, 'error': str(error)[:300]}
        with transaction() as db:
            audit(db, 'ssh.checked', asset_id, 'admin', {'reachable': True})
        return {'reachable': True}


class ConnectorExecutor:
    """Executes queued tasks for ssh assets, mirroring the agent result contract."""

    def __init__(self, transaction, audit):
        self.transaction = transaction
        self.audit = audit

    def targets(self):
        with self.transaction() as db:
            return [r['id'] for r in db.execute(
                "SELECT id FROM assets WHERE connection_type='ssh' ORDER BY id")]

    def claim(self, asset_id):
        """Take the head-of-role queued task for an ssh asset, opening a lease."""
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
            claim_id = 'connector-' + uuid.uuid4().hex
            db.execute("UPDATE tasks SET state='claimed',claim_id=?,claimed_protocol=?,lease_seconds=?,claim_count=claim_count+1,updated_at=? WHERE id=?",
                       (claim_id, PROTOCOL, LEASE_SECONDS, time.time(), row['id']))
            open_lease(db, row['id'], asset_id, claim_id)
            self.audit(db, 'task.claimed', row['id'], 'connector:' + asset_id, {'claim_id': claim_id})
            return {'id': row['id'], 'claim_id': claim_id, 'payload': json.loads(row['payload'])}

    def _connection(self, asset_id):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM asset_connections WHERE asset_id=?', (asset_id,)).fetchone()
            return dict(row) if row else None

    def run(self, asset_id, task, cancel_event):
        """Execute one claimed task and record its result. Returns the result dict."""
        from .leases import close_lease
        info = self._connection(asset_id)
        payload = task['payload']
        result = self._execute(info, payload['run_as'], payload['command'],
                               payload.get('timeout_seconds', 60), cancel_event)
        # Scrub inline output before it becomes durable or model-visible. The raw
        # archive streamed during execution remains byte-exact operator evidence.
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
            self.audit(db, 'task.result', task['id'], 'connector:' + asset_id, result)
        return result

    def _execute(self, info, run_as, command, timeout, cancel_event):
        result = {'claim_id': None, 'exit_code': None, 'stdout': '', 'stderr': '',
                  'error_code': None, 'output_truncated': False, 'status': 'failed'}
        if info is None:
            result['error_code'] = 'CONNECTION_NOT_CONFIGURED'
            return result
        try:
            client = _connect(info)
        except Exception as error:
            # Cannot even start: nothing ran, so this is a definite failure.
            result['error_code'] = _classify_connect_error(error)
            result['stderr'] = str(error)[:300] or type(error).__name__
            return result
        remote = "sudo -n -u %s -- /bin/sh -c %s" % (run_as, _shell_quote(command))
        channel = None
        try:
            channel = client.get_transport().open_session(timeout=CONNECT_TIMEOUT)
            channel.settimeout(1.0)
            channel.exec_command(remote)
        except Exception as error:
            try:
                client.close()
            except Exception:
                pass
            result['error_code'] = 'CONNECTION_FAILED'
            result['stderr'] = type(error).__name__
            return result
        out, err = bytearray(), bytearray()
        truncated = False
        cancelled = timed_out = False
        deadline = time.monotonic() + timeout
        exit_status = None
        connection_lost = False
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                moved = False
                if channel.recv_ready():
                    data = channel.recv(8192)
                    moved = True
                    if len(out) < CHUNK:
                        out.extend(data[:CHUNK - len(out)])
                    if len(out) >= CHUNK:
                        truncated = True
                if channel.recv_stderr_ready():
                    data = channel.recv_stderr(8192)
                    moved = True
                    if len(err) < CHUNK:
                        err.extend(data[:CHUNK - len(err)])
                    if len(err) >= CHUNK:
                        truncated = True
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    exit_status = channel.recv_exit_status()
                    break
                if channel.closed:
                    connection_lost = True
                    break
                if not moved:
                    time.sleep(0.05)
        except Exception:
            connection_lost = True
        result['stdout'] = bytes(out).decode('utf-8', 'replace')
        result['stderr'] = bytes(err).decode('utf-8', 'replace')
        result['output_truncated'] = truncated
        if cancelled:
            result['status'] = 'cancelled'
            result['error_code'] = 'CANCELLED_BY_OPERATOR'
        elif timed_out:
            result['status'] = 'failed'
            result['error_code'] = 'EXECUTION_TIMEOUT'
        elif connection_lost and exit_status is None:
            # We cannot confirm the far side stopped; never guess success.
            result['status'] = 'unknown'
            result['error_code'] = 'CONNECTION_LOST_DURING_EXECUTION'
        else:
            result['exit_code'] = exit_status
            result['status'] = 'succeeded' if exit_status == 0 else 'failed'
        try:
            channel.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass
        return result


def start_connector_workers(app, transaction, audit, workers=2, poll_seconds=1):
    executor = ConnectorExecutor(transaction, audit)
    app.state.connector_executor = executor
    stop = threading.Event()

    def one(asset_id):
        task = executor.claim(asset_id)
        if task is None:
            return False
        cancel_event = threading.Event()

        def watch():
            while not stop.is_set() and not cancel_event.is_set():
                with transaction() as db:
                    row = db.execute("SELECT cancel_requested_at,state FROM tasks WHERE id=?", (task['id'],)).fetchone()
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

    threads = []
    for _ in range(max(1, workers)):
        thread = threading.Thread(target=loop, name='ssh-connector', daemon=True)
        thread.start()
        threads.append(thread)
    return stop, threads
