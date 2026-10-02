"""Transactional task handoff. No LLM or arbitrary credential retrieval here."""
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")]
UserName = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z_][a-zA-Z0-9_.-]{0,63}\$?$")]
PROTOCOL = "1.0"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Role(StrictModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=200)


class Asset(StrictModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=200)
    allowed_users: list[UserName] = Field(min_length=1, max_length=100)
    notes: str = Field(default="", max_length=16000)


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


class Result(StrictModel):
    claim_id: str = Field(min_length=16, max_length=128)
    status: Literal["succeeded", "failed", "unknown"]
    exit_code: int | None = None
    stdout: str = Field(default="", max_length=65536)
    stderr: str = Field(default="", max_length=65536)
    error_code: str | None = Field(default=None, max_length=100)
    output_truncated: bool = False

    @model_validator(mode="after")
    def success_requires_zero(self):
        if self.status == "succeeded" and self.exit_code != 0:
            raise ValueError("succeeded requires exit_code=0")
        return self


SCHEMA = """
CREATE TABLE IF NOT EXISTS roles(id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, name TEXT NOT NULL, allowed_users TEXT NOT NULL, notes TEXT NOT NULL, token_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
 role_id TEXT NOT NULL REFERENCES roles(id), asset_id TEXT NOT NULL REFERENCES assets(id),
 payload TEXT NOT NULL, state TEXT NOT NULL, claim_id TEXT, result TEXT,
 idempotency_key TEXT UNIQUE NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS task_queue ON tasks(role_id,state,seq);
CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, entity_id TEXT, actor TEXT NOT NULL, details TEXT NOT NULL, created_at REAL NOT NULL);
"""


def create_app(db_path: str, admin_token: str) -> FastAPI:
    if len(admin_token) < 32:
        raise ValueError("A random admin token of at least 32 characters is required")
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise ValueError("Unsupported database schema; refusing to modify it")
        from .custom_tasks import SCHEMA as CUSTOM_SCHEMA
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + CUSTOM_SCHEMA + "\nPRAGMA user_version=2;\nCOMMIT;")

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
        db.execute("INSERT INTO audit(event,entity_id,actor,details,created_at) VALUES(?,?,?,?,?)",
                   (event, entity_id, actor, json.dumps(details, ensure_ascii=False), time.time()))

    def bearer(value):
        if not value or not value.startswith("Bearer "):
            raise HTTPException(401, "Bearer authentication required")
        return value[7:]

    def admin(authorization: str | None = Header(default=None)):
        if not hmac.compare_digest(bearer(authorization).encode(), admin_token.encode()):
            raise HTTPException(403, "Invalid administrative credential")

    def agent_auth(db, asset_id, authorization):
        row = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        candidate = hashlib.sha256(bearer(authorization).encode()).hexdigest()
        if row is None or not hmac.compare_digest(candidate, row["token_hash"]):
            raise HTTPException(403, "Invalid asset credential")
        return row

    def view(row):
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    app = FastAPI(title="AI Ops backend preview", version="0.1.0.dev2")
    from .custom_tasks import install_custom_tasks
    install_custom_tasks(app, transaction, audit, admin, admin_token)

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
            db.execute("INSERT INTO assets VALUES(?,?,?,?,?)", (body.id, body.name, json.dumps(body.allowed_users), body.notes, hashlib.sha256(token.encode()).hexdigest()))
            audit(db, "asset.provisioned", body.id, "admin", body.model_dump())
        # One-time credential: never include it in an audit event or GET route.
        return {"asset_id": body.id, "agent_token": token, "protocol_version": PROTOCOL}

    @app.get("/api/v1/assets", dependencies=[Depends(admin)])
    def list_assets():
        with transaction() as db:
            return [{"id": r["id"], "name": r["name"], "allowed_users": json.loads(r["allowed_users"]), "notes": r["notes"], "connection_type": "agent"} for r in db.execute("SELECT * FROM assets ORDER BY id")]

    @app.post("/api/v1/tasks", dependencies=[Depends(admin)], status_code=201)
    def submit(body: Task):
        payload = body.model_dump_json()
        with transaction() as db:
            old = db.execute("SELECT * FROM tasks WHERE idempotency_key=?", (body.idempotency_key,)).fetchone()
            if old:
                if json.loads(old["payload"]) != body.model_dump():
                    raise HTTPException(409, "Idempotency key reused with different payload")
                return view(old)
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
            db.execute("INSERT INTO tasks(id,role_id,asset_id,payload,state,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (task_id, body.role_id, body.asset_id, payload, state, body.idempotency_key, now, now))
            audit(db, "task.submitted", task_id, "admin", body.model_dump())
            return view(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    @app.get("/api/v1/tasks/{task_id}", dependencies=[Depends(admin)])
    def get_task(task_id: str):
        with transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Task not found")
            return view(row)

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
            changed = db.execute("UPDATE tasks SET state='cancelled',updated_at=? WHERE id=? AND state IN ('queued','awaiting_approval')", (time.time(), task_id)).rowcount
            if not changed:
                raise HTTPException(409, "Only unclaimed tasks can be cancelled in this preview; running cancellation is not implemented")
            audit(db, "task.cancelled", task_id, "admin", {})
        return {"state": "cancelled"}

    @app.post("/api/v1/agents/{asset_id}/claim")
    def claim(asset_id: Identifier, body: Claim, authorization: str | None = Header(default=None)):
        with transaction() as db:
            asset = agent_auth(db, asset_id, authorization)
            if body.protocol_version != PROTOCOL:
                raise HTTPException(409, "Unsupported protocol version")
            # Strict FIFO PER ROLE, even when earlier tasks target other assets.
            row = db.execute("""SELECT t.* FROM tasks t WHERE t.state='queued' AND t.asset_id=?
                AND NOT EXISTS (SELECT 1 FROM tasks p WHERE p.role_id=t.role_id AND p.seq<t.seq
                  AND p.state IN ('queued','awaiting_approval','claimed','unknown'))
                ORDER BY t.seq LIMIT 1""", (asset_id,)).fetchone()
            if row is None:
                return {"task": None, "protocol_version": PROTOCOL}
            payload = json.loads(row["payload"])
            if payload["run_as"] not in json.loads(asset["allowed_users"]):
                raise HTTPException(403, "Execution account is no longer available")
            claim_id = secrets.token_urlsafe(24)
            db.execute("UPDATE tasks SET state='claimed',claim_id=?,updated_at=? WHERE id=?", (claim_id, time.time(), row["id"]))
            audit(db, "task.claimed", row["id"], "agent:" + asset_id, {"claim_id": claim_id})
            return {"protocol_version": PROTOCOL, "task": {"id": row["id"], "claim_id": claim_id, **payload}}

    @app.post("/api/v1/agents/{asset_id}/tasks/{task_id}/result")
    def report_result(asset_id: Identifier, task_id: str, body: Result, authorization: str | None = Header(default=None)):
        with transaction() as db:
            agent_auth(db, asset_id, authorization)
            row = db.execute("SELECT * FROM tasks WHERE id=? AND asset_id=?", (task_id, asset_id)).fetchone()
            if row is None:
                raise HTTPException(404, "Task not found")
            if not row["claim_id"] or not hmac.compare_digest(row["claim_id"], body.claim_id):
                raise HTTPException(403, "Claim does not match")
            result = body.model_dump_json()
            if row["result"] is not None:
                if json.loads(row["result"]) == body.model_dump():
                    return {"accepted": True, "duplicate": True}
                raise HTTPException(409, "A different result is already recorded")
            if row["state"] != "claimed":
                raise HTTPException(409, "Task is not claimed")
            db.execute("UPDATE tasks SET state=?,result=?,updated_at=? WHERE id=?", (body.status, result, time.time(), task_id))
            audit(db, "task.result", task_id, "agent:" + asset_id, body.model_dump())
        return {"accepted": True, "duplicate": False}

    @app.get("/api/v1/audit", dependencies=[Depends(admin)])
    def get_audit(after: int = 0, limit: int = 100):
        if not 1 <= limit <= 500 or after < 0:
            raise HTTPException(422, "Invalid pagination")
        with transaction() as db:
            return [{**dict(r), "details": json.loads(r["details"])} for r in db.execute("SELECT * FROM audit WHERE seq>? ORDER BY seq LIMIT ?", (after, limit))]

    return app
