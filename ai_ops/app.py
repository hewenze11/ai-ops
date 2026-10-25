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
from pydantic import Field, SecretStr, model_validator

from . import scrubbing
from .models import Identifier, StrictModel, UserName, new_id

PROTOCOL = "1.0"


class Role(StrictModel):
    id: Identifier = Field(default_factory=lambda: new_id('role'))
    name: str = Field(min_length=1, max_length=200)


class Asset(StrictModel):
    id: Identifier = Field(default_factory=lambda: new_id('asset'))
    name: str = Field(min_length=1, max_length=200)
    allowed_users: list[UserName] = Field(min_length=1, max_length=100)
    account_notes: dict[str, str] | None = None
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
    ssh_secret: SecretStr | None = Field(default=None, min_length=1, max_length=65536)
    # Pinned host public key (openSSH "ssh-ed25519 AAAA..." form). The connector
    # refuses to connect when this is absent.
    ssh_host_key: str | None = Field(default=None, max_length=2000)
    ssh_key_type: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_connection(self):
        if self.connection_type == "ssh":
            if not self.ssh_host or not self.ssh_user or not self.ssh_auth_kind:
                raise ValueError("ssh assets require ssh_host, ssh_user and ssh_auth_kind")
            if bool(self.ssh_secret_ref) == bool(self.ssh_secret):
                raise ValueError("SSH assets require either a secret or an existing file reference, not both")
            if not self.ssh_host_key:
                raise ValueError("ssh assets require a pinned ssh_host_key")
        elif self.ssh_secret is not None:
            raise ValueError('Agent assets do not use an SSH password or key')
        if len(set(self.allowed_users)) != len(self.allowed_users):
            raise ValueError('Duplicate asset usernames')
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
    # Protocol 1.2 multi-controller: identifies which control plane is asking.
    # When another live controller is executing on this asset, the claim is
    # declined (empty) so each control plane queues its own work.
    controller: str | None = Field(default=None, max_length=64)


class RotateToken(StrictModel):
    # Seconds the superseded token stays valid after rotation. 0 = immediate.
    grace_seconds: int = Field(default=0, ge=0, le=86400)


class CredentialHandle(StrictModel):
    """A handle an Agent reports after a read discovered a secret (§42).

    Strict on purpose: a client that tries to include ``value`` is told it is
    wrong, rather than having the field quietly dropped. A schema that accepts
    and ignores a value field invites someone to trust that it is stored.
    """
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(default='', max_length=200)
    note: str = Field(default='', max_length=2000)
    source: str = Field(default='', max_length=1000)
    kind: Literal['pattern', 'path', 'file', 'connector'] = 'pattern'


class Result(StrictModel):
    claim_id: str = Field(min_length=16, max_length=128)
    status: Literal["succeeded", "failed", "unknown", "cancelled"]
    exit_code: int | None = None
    stdout: str = Field(default="", max_length=65536)
    stderr: str = Field(default="", max_length=65536)
    error_code: str | None = Field(default=None, max_length=100)
    output_truncated: bool = False
    output_archives: dict | None = None
    # Handles the Agent registered while running this task (§42). Ids and names
    # only: a value in this field would be a leak, and the field is unknown to
    # the model, which sees a short notice instead.
    credentials: list[CredentialHandle] = Field(default_factory=list, max_length=50)

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


def create_app(db_path: str, admin_token: str, default_model: str = "", admin_token_file: str | None = None, search_provider=None, egress=None) -> FastAPI:
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
        if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10):
            raise ValueError("Unsupported database schema; refusing to modify it")
        from .custom_tasks import SCHEMA as CUSTOM_SCHEMA
        from .turns import SCHEMA as TURN_SCHEMA, migrate, enqueue_turn, set_turn_state
        from .memory import SCHEMA as MEMORY_SCHEMA
        from .asset_memory import SCHEMA as ASSET_MEMORY_SCHEMA
        from .attachments import SCHEMA as ATTACHMENT_SCHEMA
        from .alarms import SCHEMA as ALARM_SCHEMA
        from .channels import SCHEMA as CHANNEL_SCHEMA
        from .alert_notify import SCHEMA as ALERT_NOTIFY_SCHEMA
        from .management import SCHEMA as MANAGEMENT_SCHEMA
        from .namelists import SCHEMA as NAMELISTS_SCHEMA
        from .credential_handles import SCHEMA as CREDENTIAL_SCHEMA
        from .sensitive import SCHEMA as SENSITIVE_SCHEMA
        db.execute("PRAGMA journal_mode=WAL")
        from .execution import SCHEMA as EXEC_SCHEMA, migrate as migrate_execution
        from .leases import SCHEMA as LEASE_SCHEMA, migrate as migrate_leases
        from .connector_ssh import SCHEMA as CONNECTOR_SCHEMA, migrate as migrate_connector
        from .connector_local import SCHEMA as LOCAL_SCHEMA, migrate as migrate_local
        from .asset_lifecycle import SCHEMA as LIFECYCLE_SCHEMA, migrate as migrate_lifecycle
        db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + CUSTOM_SCHEMA + TURN_SCHEMA + EXEC_SCHEMA + LEASE_SCHEMA + CONNECTOR_SCHEMA + LOCAL_SCHEMA + LIFECYCLE_SCHEMA + MEMORY_SCHEMA + ASSET_MEMORY_SCHEMA + ALARM_SCHEMA + CHANNEL_SCHEMA + ALERT_NOTIFY_SCHEMA + MANAGEMENT_SCHEMA)
        from .observability import SCHEMA as OBSERVABILITY_SCHEMA
        from .role_lifecycle import SCHEMA as ROLE_LIFECYCLE_SCHEMA
        db.executescript(OBSERVABILITY_SCHEMA + ROLE_LIFECYCLE_SCHEMA + ATTACHMENT_SCHEMA + NAMELISTS_SCHEMA + CREDENTIAL_SCHEMA + SENSITIVE_SCHEMA)
        migrate(db)
        migrate_execution(db)
        migrate_leases(db)
        migrate_connector(db)
        migrate_local(db)
        migrate_lifecycle(db)
        # Schema 7: asset credential rotation keeps a previous token hash for an
        # optional grace window so a rollout can overlap token switches.
        asset_columns = {r[1] for r in db.execute("PRAGMA table_info(assets)")}
        for name, kind in (("previous_token_hash", "TEXT"), ("previous_token_expires", "REAL")):
            if name not in asset_columns:
                db.execute("ALTER TABLE assets ADD COLUMN " + name + " " + kind)
        db.execute("PRAGMA user_version=8")
        # Schema 9: the alert-notify config gains an optional console_url so a
        # pushed conclusion can carry a tap-through link back to the console.
        # Existing installs created the table without the column.
        notify_columns = {r[1] for r in db.execute("PRAGMA table_info(alert_notify_config)")}
        if "console_url" not in notify_columns:
            db.execute("ALTER TABLE alert_notify_config ADD COLUMN console_url TEXT")
        db.execute("PRAGMA user_version=9")
        # Schema 10 (§43.1, §44.3): a turn freezes the mode and the name-list
        # ids it started under, so rebinding a list or switching a mode never
        # rewrites the history of a task that already ran. NULL means the turn
        # predates name lists and keeps platform defaults.
        turn_columns = {r[1] for r in db.execute("PRAGMA table_info(role_turns)")}
        for name in ("readonly_list", "write_list", "deny_list", "baseline_version"):
            if name not in turn_columns:
                db.execute("ALTER TABLE role_turns ADD COLUMN " + name + " TEXT")
        db.execute("PRAGMA user_version=10")
        # Seed the platform default lists once so a fresh install is never in a
        # state where a mode has no list (a missing list must not silently mean
        # "anything goes").
        from . import namelists as _namelists, baseline as _baseline
        _namelists.ensure_defaults(db, _baseline.BASELINE_VERSION)
        # Register the control host itself once. It is a normal asset with a
        # 'local' transport: no credential, no agent, but the same task, lease
        # and audit path as everything else. Disable with
        # AI_OPS_LOCAL_CONNECTOR_ENABLED=0 before startup.
        from .connector_local import ensure_local_asset
        ensure_local_asset(db)
        # Fan-out groups (§52.4). Created here so a fresh install and an
        # upgraded one end up with the same tables.
        from . import fanout as _fanout
        _fanout.migrate(db)
        from . import waiting as _waiting
        _waiting.migrate(db)
        # Do not repeat a provider call whose response was lost during a crash.
        interrupted = db.execute("SELECT id FROM role_turns WHERE state IN ('calling','tool_calling')").fetchall()
        for row in interrupted:
            set_turn_state(db, row['id'], 'failed', 'MODEL_CALL_INTERRUPTED')
            db.execute("UPDATE model_calls SET state='failed',error_code='MODEL_CALL_INTERRUPTED',finished_at=? WHERE turn_id=? AND state='calling'", (time.time(), row['id']))
            db.execute("INSERT INTO audit(event,entity_id,actor,details,created_at) VALUES('model.interrupted',?,'service','{}',?)", (row['id'], time.time()))

    @contextmanager
    def transaction(write=True):
        db = sqlite3.connect(path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        if not write:
            db.execute('PRAGMA query_only=ON')
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
    # Pydantic validation errors otherwise echo the submitted input, including
    # a write-only API key (also for unknown nested fields). Never return input.
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(request, error):
        return JSONResponse(status_code=422, content={'detail': [
            {'loc': list(item['loc']), 'type': item['type'], 'msg': 'Invalid request value'}
            for item in error.errors()]})
    app.state.transaction = transaction
    app.state.read_transaction = lambda: transaction(write=False)
    from .role_lifecycle import install as install_role_lifecycle
    install_role_lifecycle(app, transaction, audit, admin)
    app.state.audit = audit
    from .attachments import install as install_attachments
    install_attachments(app, path, admin)
    from .connector_secrets import ConnectorSecrets
    app.state.connector_secrets = ConnectorSecrets(path)
    from .role_provider import RoleProviders
    app.state.role_providers = RoleProviders(os.environ.get('AI_OPS_ROLE_SECRET_DIR') or path.parent / 'role-secrets')
    from .custom_tasks import install_custom_tasks
    from .turns import install_turns
    from .observability import install_observability
    install_observability(app, transaction, admin)
    from .unified_logs import install_unified_logs
    install_unified_logs(app, transaction, admin)
    from .log_groups import install as install_log_groups
    install_log_groups(app, admin)
    from .namelists import routes as name_list_routes
    name_list_routes(app, transaction, audit, admin)
    from .namelist_checks import install as install_namelist_checks
    install_namelist_checks(app, transaction, audit, admin)
    from .binding_impact import install as install_binding_impact
    install_binding_impact(app, transaction, admin)
    from .credential_handles import install as install_credential_handles
    install_credential_handles(app, transaction, audit, admin)
    install_custom_tasks(app, transaction, audit, admin, current_admin_token)
    install_turns(app, transaction, audit, admin, default_model, search_provider, egress)
    from .runtime_status import install as install_runtime_status
    install_runtime_status(app, admin)
    from .memory import install_memory
    install_memory(app, transaction, audit, admin)
    from .asset_memory import install as install_asset_memory
    install_asset_memory(app, transaction, audit, admin)
    from .alarms import install_alarms
    install_alarms(app, transaction, audit, admin)
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
    from .inbox import install as install_inbox
    install_inbox(app, transaction, audit, admin)
    from .sensitive import install as install_sensitive
    install_sensitive(app, transaction, audit, admin)
    from .connector_ssh import install_connector, start_connector_workers
    install_connector(app, transaction, audit, admin)
    from .connector_local import install_local_connector, start_local_connector_workers
    install_local_connector(app, transaction, audit, admin)
    from .asset_lifecycle import install_asset_lifecycle
    install_asset_lifecycle(app, transaction, audit, admin)
    from .console import install_console
    install_console(app, transaction, audit, admin)
    from .governance_console import install_governance
    app.state.governance_console = install_governance(app)
    from .channels import install_channels
    install_channels(app, transaction, audit, admin, current_admin_token)
    from .alert_notify import install_alert_notify
    install_alert_notify(app, transaction, audit, admin)
    app.state.alert_notify = {"transaction": transaction, "audit": audit}
    from .skill_import import install as install_skill_import
    install_skill_import(app, admin)
    from .management import install_management
    install_management(app, transaction, audit, admin)

    @app.get("/healthz")
    def health():
        with transaction(write=False) as db:
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
        with app.state.connector_secrets.mutation(body) as prepared, transaction() as db:
            body = prepared
            if db.execute("SELECT 1 FROM assets WHERE id=?", (body.id,)).fetchone():
                raise HTTPException(409, "Asset already exists")
            db.execute("INSERT INTO assets(id,name,allowed_users,notes,token_hash,connection_type) VALUES(?,?,?,?,?,?)", (body.id, body.name, json.dumps(body.allowed_users), body.notes, hashlib.sha256(token.encode()).hexdigest(), body.connection_type))
            if body.connection_type == "ssh":
                from .connector_ssh import save_connection
                save_connection(db, body)
            from .account_grants import save_notes
            save_notes(db, body.id, body.allowed_users, body.account_notes)
            audit(db, "asset.provisioned", body.id, "admin", {**body.model_dump(exclude={'ssh_secret', 'ssh_secret_ref'}), 'ssh_credential_configured': body.connection_type == 'ssh'})
        # One-time credential: never include it in an audit event or GET route.
        # SSH assets have no agent, so no token is issued for them.
        response = {"asset_id": body.id, "connection_type": body.connection_type, "protocol_version": PROTOCOL}
        if body.connection_type == "agent":
            response["agent_token"] = token
        return response

    @app.get("/api/v1/assets", dependencies=[Depends(admin)])
    def list_assets(include_retired: bool = False, include_disabled_local: bool = False):
        with transaction(write=False) as db:
            from .connector_ssh import connection_view
            from .observability import health_view
            from .account_grants import notes_view
            from .connector_local import local_asset_ids
            local = local_asset_ids(db)
            hidden = local['asset_id'] if (local is not None and not local['enabled']) else None
            # Operator-registered assets come first; the control host sorts last
            # so it never displaces the operator's own inventory at the top.
            local_id = local['asset_id'] if local is not None else None
            rows = [r for r in db.execute("SELECT * FROM assets ORDER BY id")
                    if include_disabled_local or r["id"] != hidden]
            # Retired assets are kept (history is evidence) but hidden from the
            # default inventory; they remain reachable by id and via lifecycle.
            rows = [r for r in rows if include_retired or r["retired_at"] is None]
            rows.sort(key=lambda r: (r["id"] == local_id, r["id"]))
            from .namelists import tags_for
            return [{"id": r["id"], "name": r["name"], "allowed_users": json.loads(r["allowed_users"]), "notes": r["notes"],
                     "connection_type": connection_view(db, r["id"])["connection_type"],
                     "retired_at": r["retired_at"],
                     "account_notes": notes_view(db, r['id']),
                     "tags": tags_for(db, r['id']),
                     "local_enabled": bool(local['enabled']) if r['id'] == local_id else None,
                     "health": health_view(db, r, app)}
                    for r in rows]

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
        with transaction(write=False) as db:
            from .connector_ssh import connection_view
            row = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Asset not found")
            from .account_grants import notes_view
            return {"id": row["id"], "name": row["name"], "account_notes": notes_view(db, row['id']), "allowed_users": json.loads(row["allowed_users"]),
                    "notes": row["notes"], "connection_type": connection_view(db, row["id"])["connection_type"],
                    "previous_token_active": bool(row["previous_token_hash"] and row["previous_token_expires"] is not None and time.time() <= row["previous_token_expires"]),
                    "previous_token_expires": row["previous_token_expires"]}

    @app.post("/api/v1/tasks", dependencies=[Depends(admin)], status_code=201)
    def submit(body: Task):
        with transaction() as db:
            old = db.execute("SELECT * FROM tasks WHERE idempotency_key=?", (body.idempotency_key,)).fetchone()
            if old:
                original = json.loads(old["payload"])
                # These fields are server-derived evidence, not caller input.
                original.pop('execution_grants', None)
                original.pop('authorization_revision', None)
                if original != body.model_dump():
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
            turn_id = enqueue_turn(db, body.role_id, "command", task_id, body.execution_users, body.mode, "Administrative command", {}, now,
                grants=[{'asset_id': body.asset_id, 'username': u} for u in body.execution_users])
            db.execute("UPDATE role_turns SET state='waiting_tool',pending_task_id=? WHERE id=?", (task_id, turn_id))
            from .account_grants import authority
            auth = authority(db, turn_id)
            evidence = {**body.model_dump(), 'execution_grants': auth['execution_grants'],
                        'authorization_revision': auth['authorization_revision']}
            payload = json.dumps(evidence, ensure_ascii=False)
            db.execute("INSERT INTO tasks(id,role_id,asset_id,payload,state,idempotency_key,created_at,updated_at,turn_id) VALUES(?,?,?,?,?,?,?,?,?)",
                       (task_id, body.role_id, body.asset_id, payload, state, body.idempotency_key, now, now, turn_id))
            audit(db, "task.submitted", task_id, "admin", {'turn_id': turn_id, **evidence})
            return view(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone(), db)

    @app.get("/api/v1/tasks/{task_id}", dependencies=[Depends(admin)])
    def get_task(task_id: str):
        with transaction(write=False) as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Task not found")
            return view(row, db)

    @app.post("/api/v1/tasks/{task_id}/approve", dependencies=[Depends(admin)])
    def approve(task_id: str, body: dict | None = None):
        note = str((body or {}).get('reason') or '')[:2000]
        with transaction() as db:
            changed = db.execute("UPDATE tasks SET state='queued',updated_at=? WHERE id=? AND state='awaiting_approval'", (time.time(), task_id)).rowcount
            if not changed:
                raise HTTPException(409, "Task is not awaiting approval")
            audit(db, "task.approved", task_id, "admin", {'reason': note} if note else {})
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
            if body.protocol_version not in ('1.0', '1.1', '1.2', '1.3'):
                raise HTTPException(409, "Unsupported protocol version")
            # Protocol 1.2 multi-controller: if a DIFFERENT live controller is
            # executing on this asset, decline so each control plane queues its
            # own work. A controller never blocks itself (its own earlier task
            # is already 'claimed', so the query below would not return it).
            if body.controller is not None:
                db.execute('INSERT INTO controller_observations(asset_id,controller,observed_at) VALUES(?,?,?) ON CONFLICT(asset_id) DO UPDATE SET controller=excluded.controller,observed_at=excluded.observed_at', (asset_id, body.controller, time.time()))
                presence = db.execute('SELECT * FROM agent_presence WHERE asset_id=?', (asset_id,)).fetchone()
                if presence is not None and presence['busy'] and time.time() - presence['last_seen'] <= 30 \
                        and presence['busy_by'] not in (None, body.controller):
                    return {"task": None, "protocol_version": body.protocol_version,
                            "busy_by": presence['busy_by']}
            # FIFO is now by parent ROLE TURN, not command insertion order.
            # A multi-step model turn may add a child after a later manual turn.
            #
            # Within one turn, only readonly work may overlap (§52.8). The
            # earlier blanket rule ("one unfinished task per turn") also
            # serialised reads the model issued back to back, which is the
            # whole point of letting it fan in. It cannot simply be dropped:
            # a read overlapping a change means the model is reading a host
            # while something is editing it, and the result is uninterpretable.
            # Approvals and writes keep the old rule untouched -- a turn still
            # has exactly one unfinished write intent at a time.
            row = db.execute("""SELECT t.* FROM tasks t JOIN role_turns rt ON rt.id=t.turn_id
                WHERE t.state='queued' AND t.asset_id=? AND rt.state IN ('waiting_tool','waiting_task')
                AND NOT EXISTS (SELECT 1 FROM role_turns p WHERE p.role_id=rt.role_id AND p.seq<rt.seq
                  AND p.state NOT IN ('completed','failed','cancelled'))
                AND NOT EXISTS (SELECT 1 FROM tasks prior WHERE prior.turn_id=t.turn_id AND prior.seq<t.seq
                  AND prior.state IN ('queued','awaiting_approval','claimed','unknown')
                  AND json_extract(prior.payload,'$.kind') IS NOT 'fanout'
                  AND NOT (json_extract(t.payload,'$.kind') = 'readonly'
                           AND json_extract(prior.payload,'$.kind') = 'readonly'))
                AND json_extract(t.payload,'$.kind') IS NOT 'fanout'
                ORDER BY rt.seq,t.seq LIMIT 1""", (asset_id,)).fetchone()
            # A reveal task has no parent turn: it is dispatched by an operator
            # looking at a completed read, not by the model. It is never queued
            # ahead of model work, and it is claimed only when nothing else is
            # waiting -- a human staring at a card must not jump the queue in
            # front of the turn that is actually being diagnosed.
            if row is None:
                reveal_row = db.execute(
                    "SELECT * FROM tasks WHERE state='queued' AND asset_id=? AND turn_id IS NULL "
                    "AND idempotency_key LIKE 'reveal:%' ORDER BY seq LIMIT 1", (asset_id,)).fetchone()
                if reveal_row is not None:
                    busy = db.execute("SELECT 1 FROM tasks WHERE asset_id=? AND state IN "
                                      "('awaiting_approval','claimed','unknown') LIMIT 1", (asset_id,)).fetchone()
                    if busy is None:
                        row = reveal_row
            # A fan-out parent is a bookkeeping row, not work for any host: it
            # carries no argv and exists only so the turn can wait on one id.
            # Claiming it here would leave a real Agent with nothing to run.
            if row is not None and json.loads(row['payload'] or '{}').get('kind') == 'fanout':
                row = None
            # Protocol 1.3 kinds (readonly/change) carry structured payloads an
            # older agent would misread. Leave the task queued and tell the
            # console which agent needs upgrading instead of shipping it.
            if row is not None and body.protocol_version not in ('1.3',):
                head = json.loads(row['payload'])
                if head.get('kind', 'shell') != 'shell':
                    blockers = [r[0] for r in db.execute(
                        "SELECT id, payload FROM tasks WHERE asset_id=? AND state='queued' ORDER BY seq LIMIT 5", (asset_id,))
                        if json.loads(r[1]).get('kind', 'shell') != 'shell']
                    self_blocked = db.execute('SELECT 1 FROM blocked_kinds WHERE asset_id=? AND protocol=? AND reported_at>?',
                                              (asset_id, body.protocol_version, time.time() - 300)).fetchone()
                    if self_blocked is None:
                        db.execute('INSERT INTO blocked_kinds(asset_id,protocol,task_ids,reported_at) VALUES(?,?,?,?)',
                                   (asset_id, body.protocol_version, json.dumps(blockers), time.time()))
                        audit(db, 'task.kind_blocked', asset_id, 'agent:' + asset_id,
                              {'protocol': body.protocol_version, 'task_ids': blockers})
                    return {"task": None, "protocol_version": body.protocol_version,
                            "blocked_kind": True, "blocked_task_ids": blockers}
            if row is None:
                return {"task": None, "protocol_version": body.protocol_version}
            payload = json.loads(row["payload"])
            from .authority import claim_allowed
            if not claim_allowed(db, row):
                raise HTTPException(403, "Execution authority is no longer available")
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
            #
            # Exception, and it is the only one: a `reveal` task is dispatched by
            # an operator, not by the model, and its whole purpose is to show
            # unmasked bytes to the operator. Scrubbing it here would make the
            # feature a no-op (that was the bug in the first cut). The bytes do
            # not go where model output goes: see `reveal_secret` below. Every
            # other kind is scrubbed exactly as before.
            task_kind = 'shell'
            task_payload = {}
            _payload_row = db.execute('SELECT payload FROM tasks WHERE id=?', (task_id,)).fetchone()
            if _payload_row and _payload_row['payload']:
                try:
                    task_payload = json.loads(_payload_row['payload']) or {}
                    task_kind = task_payload.get('kind', 'shell')
                except ValueError:
                    task_payload, task_kind = {}, 'shell'
            if task_kind != 'reveal':
                body = Result(**scrubbing.redact_result(body.model_dump()))
            # A credential handle the Agent reported becomes an inventory entry
            # here, next to the host that found it (§42, §46.3). Values never
            # travel: the result schema has no field for one.
            if body.credentials:
                from .credential_handles import register as register_handles
                register_handles(db, asset_id, [{'id': str(c.id)[:200],
                                                 'name': str(c.name or c.id)[:200],
                                                 'note': c.note[:2000],
                                                 'source': c.source[:1000],
                                                 'kind': c.kind}
                                                for c in body.credentials if c.id])
            result = body.model_dump_json()
            if row["result"] is not None:
                if Result.model_validate_json(row['result']).model_dump() == body.model_dump():
                    return {"accepted": True, "duplicate": True}
                raise HTTPException(409, "A different result is already recorded")
            if row["state"] != "claimed":
                raise HTTPException(409, "Task is not claimed")
            if body.status == 'cancelled' and (row['claimed_protocol'] not in ('1.1', '1.2') or row['cancel_requested_at'] is None):
                raise HTTPException(409, 'Cancelled outcome requires an acknowledged cancellation request')
            check_archives(db, task_id, body.output_archives)
            close_lease(db, task_id)
            # A child of a fan-out reports like any other task, but the group
            # needs to know before the turn loop looks at it. Recording here
            # means the parent can complete on the same pass that the last
            # child lands, instead of one poll later.
            try:
                from .fanout_runner import FanoutRunner
                FanoutRunner(self.transaction, audit, None).child_reported(db, task_id)
            except Exception:
                # Fan-out bookkeeping must never reject a task result: the
                # result is the durable evidence, the group state is derived.
                pass
            # §51.4: a masked read produces a hit the operator can unlock. The
            # value never lands in this table -- only the rule ids and the
            # path -- so unlocking has to re-read the host.
            if body.status in ('succeeded', 'failed'):
                from . import sensitive
                from .scrubbing import MASK, secret_kinds
                if task_payload.get('kind') == 'readonly':
                    text = (body.stdout or '') + '\n' + (body.stderr or '')
                    if MASK in text:
                        rules = secret_kinds(text)
                        if rules:
                            source = next((a for a in (task_payload.get('args') or [])
                                           if a.startswith('/')), '')
                            hit = sensitive.record(db, turn_id=row['turn_id'], task_id=task_id,
                                                   asset_id=asset_id, path=source,
                                                   rule_ids=rules,
                                                   baseline_version=sensitive._baseline_version())
                            audit(db, 'sensitive.hit', hit, 'service',
                                  {'turn_id': row['turn_id'], 'path': source,
                                   'rule_ids': rules, 'task_id': task_id})
            # A reveal result is unmasked, so it must not land in tasks.result:
            # that column is what the model is handed as tool output and what
            # audit views read. Park the bytes in reveal_secret instead and store
            # a stub here, so nothing downstream can pick them up by accident.
            if task_kind == 'reveal':
                sensitive.stash_reveal(db, task_id=task_id, hit_id=(task_payload or {}).get('hit_id'),
                                       stdout=(body.stdout or '')[:sensitive.MAX_STASH],
                                       stderr=(body.stderr or '')[:sensitive.MAX_STASH])
                audit(db, 'sensitive.reveal_completed', task_id, 'agent:' + asset_id,
                      {'hit_id': (task_payload or {}).get('hit_id'),
                       'status': body.status, 'bytes': len(body.stdout or ''),
                       'note': 'operator view only; not model-visible'})
                body = Result(claim_id=body.claim_id, status=body.status, exit_code=body.exit_code,
                              stdout='[operator view only -- see the unlock card]',
                              stderr='', error_code=body.error_code,
                              output_truncated=body.output_truncated)
                result = body.model_dump_json()
            db.execute("UPDATE tasks SET state=?,result=?,updated_at=? WHERE id=?", (body.status, result, time.time(), task_id))
            parent = db.execute("SELECT * FROM role_turns WHERE id=?", (row['turn_id'],)).fetchone()
            if parent is None:
                # A task not created by a turn (legacy import or a direct admin
                # command) still records its result; only turn bookkeeping is
                # skipped.
                audit(db, "task.result", task_id, "agent:" + asset_id, body.model_dump())
                return {"accepted": True, "duplicate": False}
            if body.status == 'unknown':
                set_turn_state(db, parent['id'], 'blocked_unknown', 'EXECUTION_UNKNOWN')
            elif body.status == 'cancelled':
                # Two situations share the 'cancelled' task state, and conflating
                # them breaks the wait design:
                #  * an ordinary cancel -- the turn ends, as before;
                #  * a job abandoned mid-wait whose cancellation reached the host
                #    before it finished -- the user only stopped waiting and the
                #    turn is already thinking again. Cancelling it here would end
                #    a conversation because a build was interrupted.
                still = db.execute('SELECT state FROM role_turns WHERE id=?', (parent['id'],)).fetchone()
                if still is None or still['state'] != 'ready':
                    set_turn_state(db, parent['id'], 'cancelled')
            elif row['cancel_requested_at'] is not None:
                # A completion racing with cancel remains an honest task result,
                # but no more model tools run in this cancelled parent turn.
                set_turn_state(db, parent['id'], 'cancelled')
            elif parent['source'] == 'command':
                set_turn_state(db, parent['id'], 'completed' if body.status == 'succeeded' else 'failed')
            audit(db, "task.result", task_id, "agent:" + asset_id, body.model_dump())
        return {"accepted": True, "duplicate": False}

    @app.get("/api/v1/audit", dependencies=[Depends(admin)])
    def get_audit(after: int = 0, limit: int = 100, entity_id: str | None = None,
                  event_prefix: str | None = None, actor: str | None = None, before: int | None = None):
        """Audit stream. Filters are plain equality / prefix so the console can
        show one host's or one turn's timeline without a second endpoint.
        ``before`` pages backwards (newest first) for a timeline view."""
        if not 1 <= limit <= 500 or after < 0:
            raise HTTPException(422, "Invalid pagination")
        clauses, args = [], []
        if before is not None:
            clauses.append('seq<?')
            args.append(before)
        else:
            clauses.append('seq>?')
            args.append(after)
        if entity_id:
            clauses.append('entity_id=?')
            args.append(entity_id[:200])
        if event_prefix:
            clauses.append("event LIKE ? ESCAPE '\\'")
            args.append(event_prefix[:100].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
        if actor:
            clauses.append('actor=?')
            args.append(actor[:100])
        order = 'DESC' if before is not None else 'ASC'
        with transaction() as db:
            return [{**dict(r), "details": json.loads(r["details"])} for r in db.execute(
                "SELECT * FROM audit WHERE " + ' AND '.join(clauses) + " ORDER BY seq " + order + " LIMIT ?", (*args, limit))]

    return app
