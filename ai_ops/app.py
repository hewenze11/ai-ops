"""Transactional task handoff. No LLM or arbitrary credential retrieval here."""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import Field, model_validator

from . import scrubbing
from .models import Identifier, StrictModel, UserName

PROTOCOL = "1.0"


class Role(StrictModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=200)


class Asset(StrictModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=200)
    allowed_users: list[UserName] = Field(min_length=1, max_length=100)
    notes: str = Field(default="", max_length=16000)
    connection_type: Literal["agent", "ssh"] = "agent"
    # Only meaningful for connection_type="ssh". Credentials are never stored
    # inline here; ssh_host/ssh_port/ssh_user/ssh_auth_kind describe the endpoint
    # and the secret itself lives in asset_connections.secret_ref.
    ssh_host: str | None = Field(default=None, max_length=255)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str | None = Field(default=None, max_length=64)
    ssh_auth_kind: Literal["key", "password"] | None = None
    ssh_secret_ref: str | None = Field(default=None, max_length=512)
    # Pinned host public key (openSSH "ssh-ed25519 AAAA..." form). The connector
    # refuses to connect when this is absent.
    ssh_host_key: str | None = Field(default=None, max_length=2000)
    ssh_key_type: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_connection(self):
        if self.connection_type == "ssh":
            if not self.ssh_host or not self.ssh_user or not self.ssh_auth_kind:
                raise ValueError("ssh assets require ssh_host, ssh_user and ssh_auth_kind")
            if not self.ssh_secret_ref:
                raise ValueError("ssh assets require ssh_secret_ref")
            if not self.ssh_host_key:
                raise ValueError("ssh assets require a pinned ssh_host_key")
        return self


class Task(StrictModel):
    role_id: Identifier
    asset_id: Identifier
    execution_users: list[UserName] = Field(min_length=1, max_length=100)
    run_as: UserName
    command: str = Field(min_length=1, max_length=16000)
    timeout_seconds: int = Field(default=60, ge=1, le=3600)
    mode: Literal["direct", "confirm"] = "direct"
    idempotency_key: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def validate_selected_user(self):
        if self.run_as not in self.execution_users:
            raise ValueError("run_as must be in this task's execution_users")
        if len(set(self.execution_users)) != len(self.execution_users):
            raise ValueError("execution_users must not contain duplicates")
        return self


class Claim(StrictModel):
    protocol_version: str


class RotateToken(StrictModel):
    # Seconds the superseded token stays valid after rotation. 0 = immediate.
    grace_seconds: int = Field(default=0, ge=0, le=86400)


class Result(StrictModel):
    claim_id: str = Field(min_length=16, max_length=128)
    status: Literal["succeeded", "failed", "unknown", "cancelled"]
    exit_code: int | None = None
    stdout: str = Field(default="", max_length=65536)
    stderr: str = Field(default="", max_length=65536)
    error_code: str | None = Field(default=None, max_length=100)
    output_truncated: bool = False
    output_archives: dict | None = None

    @model_validator(mode="after")
    def success_requires_zero(self):
        if self.status == "succeeded" and self.exit_code != 0:
            raise ValueError("succeeded requires exit_code=0")
        return self


SCHEMA = """
CREATE TABLE IF NOT EXISTS roles(id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, name TEXT NOT NULL, allowed_users TEXT NOT NULL, notes TEXT NOT NULL, token_hash TEXT NOT NULL, connection_type TEXT NOT NULL DEFAULT 'agent', previous_token_hash TEXT, previous_token_expires REAL);
CREATE TABLE IF NOT EXISTS tasks(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
 role_id TEXT NOT NULL REFERENCES roles(id), asset_id TEXT NOT NULL REFERENCES assets(id),
 payload TEXT NOT NULL, state TEXT NOT NULL, claim_id TEXT, result TEXT,
 idempotency_key TEXT UNIQUE NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS task_queue ON tasks(role_id,state,seq);
CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, entity_id TEXT, actor TEXT NOT NULL, details TEXT NOT NULL, created_at REAL NOT NULL);
"""


def create_app(db_path: str, admin_token: str, default_model: str = "", admin_token_file: str | None = None, search_provider=None) -> FastAPI:
    if len(admin_token) < 32:
        raise ValueError("A random admin token of at least 32 characters is required")
    path = Path(db_path)

    def current_admin_token():
        # When the token is backed by a file we re-read it on every check so an
        # operator can rotate the credential by replacing the file, without a
        # restart. If the file is missing or unreadable we fall back to the
        # token loaded at startup rather than locking the operator out.
        if admin_token_file:
            try:
                value = Path(admin_token_file).read_text().strip()
                if len(value) >= 32:
                    return value
            except OSError:
                pass
        return admin_token

    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5, 6, 7):
            raise ValueError("Unsupported database schema; refusing to modify it")
        from .custom_tasks import SCHEMA as CUSTOM_SCHEMA
        from .turns import SCHEMA as TURN_SCHEMA, migrate, enqueue_turn, set_turn_state
        from .memory import SCHEMA as MEMORY_SCHEMA
        db.execute("PRAGMA journal_mode=WAL")
        from .execution import SCHEMA as EXEC_SCHEMA, migrate as migrate_execution
        from .leases import SCHEMA as LEASE_SCHEMA, migrate as migrate_leases
        from .connector_ssh import SCHEMA as CONNECTOR_SCHEMA, migrate as migrate_connector
        db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + CUSTOM_SCHEMA + TURN_SCHEMA + EXEC_SCHEMA + LEASE_SCHEMA + CONNECTOR_SCHEMA + MEMORY_SCHEMA)
        migrate(db)
        migrate_execution(db)
        migrate_leases(db)
        migrate_connector(db)
        # Schema 7: asset credential rotation keeps a previous token hash for an
        # optional grace window so a rollout can overlap token switches.
        asset_columns = {r[1] for r in db.execute("PRAGMA table_info(assets)")}
        for name, kind in (("previous_token_hash", "TEXT"), ("previous_token_expires", "REAL")):
            if name not in asset_columns:
                db.execute("ALTER TABLE assets ADD COLUMN " + name + " " + kind)
        db.execute("PRAGMA user_version=7")
        # Do not repeat a provider call whose response was lost during a crash.
        interrupted = db.execute("SELECT id FROM role_turns WHERE state='calling'").fetchall()
        for row in interrupted:
            set_turn_state(db, row['id'], 'failed', 'MODEL_CALL_INTERRUPTED')
            db.execute("UPDATE model_calls SET state='failed',error_code='MODEL_CALL_INTERRUPTED',finished_at=? WHERE turn_id=? AND state='calling'", (time.time(), row['id']))
            db.execute("INSERT INTO audit(event,entity_id,actor,details,created_at) VALUES('model.interrupted',?,'service','{}',?)", (row['id'], time.time()))

    @contextmanager
    def transaction():
        db = sqlite3.connect(path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def audit(db, event, entity_id, actor, details):
        # This insert is in the SAME transaction as the state change. Failure
        # blocks dispatch; it is not a best-effort logger or LLM-written memory.
        # Details are scrubbed so a command or payload echoed into an audit event
        # cannot persist a secret into the audit trail.
        safe = scrubbing.redact_structure(details) if details else details
        db.execute("INSERT INTO audit(event,entity_id,actor,details,created_at) VALUES(?,?,?,?,?)",
                   (event, entity_id, actor, json.dumps(safe, ensure_ascii=False), time.time()))

    def bearer(value):
        if not value or not value.startswith("Bearer "):
            raise HTTPException(401, "Bearer authentication required")
        return value[7:]

    def admin(authorization: str | None = Header(default=None)):
        if not hmac.compare_digest(bearer(authorization).encode(), current_admin_token().encode()):
            raise HTTPException(403, "Invalid administrative credential")

    def agent_auth(db, asset_id, authorization):
        row = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        candidate = hashlib.sha256(bearer(authorization).encode()).hexdigest()
        if row is None:
            raise HTTPException(403, "Invalid asset credential")
        if hmac.compare_digest(candidate, row["token_hash"]):
            return row
        # A previous token stays valid only inside its grace window (if any).
        previous = row["previous_token_hash"]
        expires = row["previous_token_expires"]
        if previous and expires is not None and time.time() <= expires and hmac.compare_digest(candidate, previous):
            return row
        raise HTTPException(403, "Invalid asset credential")

    from .leases import LEASE_SECONDS, lease_view, open_lease, close_lease

    def view(row, db=None):
        # Callers pass the transaction they already hold; opening a second
        # connection here would self-deadlock on SQLite's write lock.
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        if db is not None:
            result["lease"] = lease_view(db, row["id"])
        return result

    app = FastAPI(title="AI Ops backend preview", version="0.1.0.dev6")
    app.state.transaction = transaction
    app.state.audit = audit
    from .custom_tasks import install_custom_tasks
    from .turns import install_turns
    install_custom_tasks(app, transaction, audit, admin, current_admin_token)
    install_turns(app, transaction, audit, admin, default_model, search_provider)
    from .memory import install_memory
    install_memory(app, transaction, audit, admin)
    from .execution import install_execution, cancel_task, check_archives
    from .retention import install_retention, policy_from_env
    retention_policy = policy_from_env(os.environ)
    install_execution(app, transaction, audit, admin, agent_auth, retention_policy)
    install_retention(app, transaction, audit, admin, retention_policy)
    app.state.retention_policy = retention_policy
    from .backup import install_backup
    install_backup(app, transaction, audit, admin, str(path))
    from .leases import install_leases
    install_leases(app, transaction, audit, admin)
    from .connector_ssh import install_connector, start_connector_workers
    install_connector(app, transaction, audit, admin)

    @app.get("/healthz")
    def health():
        with transaction() as db:
            db.execute("SELECT 1")
        return {"status": "ok", "protocol_version": PROTOCOL}

    @app.post("/api/v1/roles", dependencies=[Depends(admin)], status_code=201)
    def create_role(body: Role):
        with transaction() as db:
            if db.execute("SELECT 1 FROM roles WHERE id=?", (body.id,)).fetchone():
                raise HTTPException(409, "Role already exists")
            db.execute("INSERT INTO roles VALUES(?,?)", (body.id, body.name))
            audit(db, "role.created", body.id, "admin", body.model_dump())
        return body

    @app.post("/api/v1/assets", dependencies=[Depends(admin)], status_code=201)
    def provision_asset(body: Asset):
        token = secrets.token_urlsafe(36)
        with transaction() as db:
            if db.execute("SELECT 1 FROM assets WHERE id=?", (body.id,)).fetchone():
                raise HTTPException(409, "Asset already exists")
            db.execute("INSERT INTO assets(id,name,allowed_users,notes,token_hash,connection_type) VALUES(?,?,?,?,?,?)", (body.id, body.name, json.dumps(body.allowed_users), body.notes, hashlib.sha256(token.encode()).hexdigest(), body.connection_type))
            if body.connection_type == "ssh":
                from .connector_ssh import save_connection
                save_connection(db, body)
            audit(db, "asset.provisioned", body.id, "admin", {**body.model_dump(), "ssh_secret_ref": "[redacted]"})
        # One-time credential: never include it in an audit event or GET route.
        # SSH assets have no agent, so no token is issued for them.
        response = {"asset_id": body.id, "connection_type": body.connection_type, "protocol_version": PROTOCOL}
        if body.connection_type == "agent":
            response["agent_token"] = token
        return response

    @app.get("/api/v1/assets", dependencies=[Depends(admin)])
    def list_assets():
        with transaction() as db:
            from .connector_ssh import connection_view
            return [{"id": r["id"], "name": r["name"], "allowed_users": json.loads(r["allowed_users"]), "notes": r["notes"],
                     "connection_type": connection_view(db, r["id"])["connection_type"]} for r in db.execute("SELECT * FROM assets ORDER BY id")]

    @app.post("/api/v1/assets/{asset_id}/rotate-token", dependencies=[Depends(admin)])
    def rotate_asset_token(asset_id: str, body: RotateToken):
        token = secrets.token_urlsafe(36)
        now = time.time()
        with transaction() as db:
            row = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Asset not found")
            if row["connection_type"] != "agent":
                raise HTTPException(409, "Only agent assets carry a rotatable token")
            # Keep the old hash only for the grace window; otherwise drop it so a
            # superseded token is invalid immediately.
            if body.grace_seconds > 0:
                db.execute("UPDATE assets SET token_hash=?,previous_token_hash=?,previous_token_expires=? WHERE id=?",
                           (hashlib.sha256(token.encode()).hexdigest(), row["token_hash"], now + body.grace_seconds, asset_id))
            else:
                db.execute("UPDATE assets SET token_hash=?,previous_token_hash=NULL,previous_token_expires=NULL WHERE id=?",
                           (hashlib.sha256(token.encode()).hexdigest(), asset_id))
            audit(db, "asset.token_rotated", asset_id, "admin",
                  {"grace_seconds": body.grace_seconds})
        # The new credential is returned exactly once and never audited.
        return {"asset_id": asset_id, "agent_token": token, "grace_seconds": body.grace_seconds}

    @app.get("/api/v1/assets/{asset_id}", dependencies=[Depends(admin)])
    def get_asset(asset_id: str):
        with transaction() as db:
            from .connector_ssh import connection_view
            row = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Asset not found")
            return {"id": row["id"], "name": row["name"], "allowed_users": json.loads(row["allowed_users"]),
                    "notes": row["notes"], "connection_type": connection_view(db, row["id"])["connection_type"],
                    "previous_token_active": bool(row["previous_token_hash"] and row["previous_token_expires"] is not None and time.time() <= row["previous_token_expires"]),
                    "previous_token_expires": row["previous_token_expires"]}

    @app.post("/api/v1/tasks", dependencies=[Depends(admin)], status_code=201)
    def submit(body: Task):
        payload = body.model_dump_json()
        with transaction() as db:
            old = db.execute("SELECT * FROM tasks WHERE idempotency_key=?", (body.idempotency_key,)).fetchone()
            if old:
                if json.loads(old["payload"]) != body.model_dump():
                    raise HTTPException(409, "Idempotency key reused with different payload")
                return view(old, db)
            if not db.execute("SELECT 1 FROM roles WHERE id=?", (body.role_id,)).fetchone():
                raise HTTPException(404, "Role not found")
            asset = db.execute("SELECT * FROM assets WHERE id=?", (body.asset_id,)).fetchone()
            if not asset:
                raise HTTPException(404, "Asset not found")
            if body.run_as not in json.loads(asset["allowed_users"]):
                raise HTTPException(403, "Actual execution user is not configured for this asset")
            now = time.time()
            task_id = str(uuid.uuid4())
            state = "awaiting_approval" if body.mode == "confirm" else "queued"
            turn_id = enqueue_turn(db, body.role_id, "command", task_id, body.execution_users, body.mode, "Administrative command", {}, now)
            db.execute("UPDATE role_turns SET state='waiting_tool',pending_task_id=? WHERE id=?", (task_id, turn_id))
            db.execute("INSERT INTO tasks(id,role_id,asset_id,payload,state,idempotency_key,created_at,updated_at,turn_id) VALUES(?,?,?,?,?,?,?,?,?)",
                       (task_id, body.role_id, body.asset_id, payload, state, body.idempotency_key, now, now, turn_id))
            audit(db, "task.submitted", task_id, "admin", body.model_dump())
            return view(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone(), db)

    @app.get("/api/v1/tasks/{task_id}", dependencies=[Depends(admin)])
    def get_task(task_id: str):
        with transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Task not found")
            return view(row, db)

    @app.post("/api/v1/tasks/{task_id}/approve", dependencies=[Depends(admin)])
    def approve(task_id: str):
        with transaction() as db:
            changed = db.execute("UPDATE tasks SET state='queued',updated_at=? WHERE id=? AND state='awaiting_approval'", (time.time(), task_id)).rowcount
            if not changed:
                raise HTTPException(409, "Task is not awaiting approval")
            audit(db, "task.approved", task_id, "admin", {})
        return {"state": "queued"}

    @app.post("/api/v1/tasks/{task_id}/cancel", dependencies=[Depends(admin)])
    def cancel(task_id: str):
        with transaction() as db:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if task is None:
                raise HTTPException(404, 'Task not found')
            state = cancel_task(db, audit, task)
            from .leases import close_lease
            if state == 'cancelled':
                set_turn_state(db, task['turn_id'], 'cancelled')
                close_lease(db, task_id)
                audit(db, 'task.cancelled', task_id, 'admin', {})
        return {'state': state}

    @app.post("/api/v1/agents/{asset_id}/claim")
    def claim(asset_id: Identifier, body: Claim, authorization: str | None = Header(default=None)):
        with transaction() as db:
            asset = agent_auth(db, asset_id, authorization)
            if body.protocol_version not in ('1.0', '1.1'):
                raise HTTPException(409, "Unsupported protocol version")
            # FIFO is now by parent ROLE TURN, not command insertion order.
            # A multi-step model turn may add a child after a later manual turn.
            row = db.execute("""SELECT t.* FROM tasks t JOIN role_turns rt ON rt.id=t.turn_id
                WHERE t.state='queued' AND t.asset_id=? AND rt.state='waiting_tool'
                AND NOT EXISTS (SELECT 1 FROM role_turns p WHERE p.role_id=rt.role_id AND p.seq<rt.seq
                  AND p.state NOT IN ('completed','failed','cancelled'))
                AND NOT EXISTS (SELECT 1 FROM tasks prior WHERE prior.turn_id=t.turn_id AND prior.seq<t.seq
                  AND prior.state IN ('queued','awaiting_approval','claimed','unknown'))
                ORDER BY rt.seq,t.seq LIMIT 1""", (asset_id,)).fetchone()
            if row is None:
                return {"task": None, "protocol_version": body.protocol_version}
            payload = json.loads(row["payload"])
            if payload["run_as"] not in json.loads(asset["allowed_users"]):
                raise HTTPException(403, "Execution account is no longer available")
            claim_id = secrets.token_urlsafe(24)
            db.execute("UPDATE tasks SET state='claimed',claim_id=?,claimed_protocol=?,lease_seconds=?,claim_count=claim_count+1,updated_at=? WHERE id=?", (claim_id, body.protocol_version, LEASE_SECONDS, time.time(), row["id"]))
            open_lease(db, row['id'], asset_id, claim_id)
            audit(db, "task.claimed", row["id"], "agent:" + asset_id, {"claim_id": claim_id})
            return {"protocol_version": body.protocol_version, "task": {"id": row["id"], "claim_id": claim_id, **payload}}

    @app.post("/api/v1/agents/{asset_id}/tasks/{task_id}/result")
    def report_result(asset_id: Identifier, task_id: str, body: Result, authorization: str | None = Header(default=None)):
        with transaction() as db:
            agent_auth(db, asset_id, authorization)
            row = db.execute("SELECT * FROM tasks WHERE id=? AND asset_id=?", (task_id, asset_id)).fetchone()
            if row is None:
                raise HTTPException(404, "Task not found")
            if not row["claim_id"] or not hmac.compare_digest(row["claim_id"], body.claim_id):
                raise HTTPException(403, "Claim does not match")
            # Inline stdout/stderr is handed to the model as tool output and
            # shown in audit views, so scrub it at the boundary; every downstream
            # step (duplicate comparison, stored result, audit) then sees the
            # same redacted value. The raw archive (digest-verified) is left
            # byte-exact on purpose.
            body = Result(**scrubbing.redact_result(body.model_dump()))
            result = body.model_dump_json()
            if row["result"] is not None:
                if Result.model_validate_json(row['result']).model_dump() == body.model_dump():
                    return {"accepted": True, "duplicate": True}
                raise HTTPException(409, "A different result is already recorded")
            if row["state"] != "claimed":
                raise HTTPException(409, "Task is not claimed")
            if body.status == 'cancelled' and (row['claimed_protocol'] != '1.1' or row['cancel_requested_at'] is None):
                raise HTTPException(409, 'Cancelled outcome requires an acknowledged cancellation request')
            check_archives(db, task_id, body.output_archives)
            close_lease(db, task_id)
            db.execute("UPDATE tasks SET state=?,result=?,updated_at=? WHERE id=?", (body.status, result, time.time(), task_id))
            parent = db.execute("SELECT * FROM role_turns WHERE id=?", (row['turn_id'],)).fetchone()
            if body.status == 'unknown':
                set_turn_state(db, parent['id'], 'blocked_unknown', 'EXECUTION_UNKNOWN')
            elif body.status == 'cancelled' or row['cancel_requested_at'] is not None:
                # A completion racing with cancel remains an honest task result,
                # but no more model tools run in this cancelled parent turn.
                set_turn_state(db, parent['id'], 'cancelled')
            elif parent['source'] == 'command':
                set_turn_state(db, parent['id'], 'completed' if body.status == 'succeeded' else 'failed')
            audit(db, "task.result", task_id, "agent:" + asset_id, body.model_dump())
        return {"accepted": True, "duplicate": False}

    @app.get("/api/v1/audit", dependencies=[Depends(admin)])
    def get_audit(after: int = 0, limit: int = 100):
        if not 1 <= limit <= 500 or after < 0:
            raise HTTPException(422, "Invalid pagination")
        with transaction() as db:
            return [{**dict(r), "details": json.loads(r["details"])} for r in db.execute("SELECT * FROM audit WHERE seq>? ORDER BY seq LIMIT ?", (after, limit))]

    return app
