"""Role-turn queue and model orchestration, independent of the Linux agent."""
import json
import time
import uuid
from typing import Annotated, Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .context import SYSTEM, build_body
from .model_client import ModelFailure
from .models import Identifier as ID, UserName as USER
TERMINAL = ("completed", "failed", "cancelled")


def refresh_day_memory(db, turn):
    """Re-materialise this turn's day memory the moment the turn completes.

    Without this, a day row is only rebuilt lazily at the NEXT model invocation,
    so the just-finished turn is missing from memory until then. That lag is a
    real coherence bug: a follow-up question asking "what did you just do?"
    would not see the answer in memory. Rebuilding on completion keeps the
    archived day equal to the turns actually completed so far. Best effort: a
    memory failure must never fail the turn itself.
    """
    try:
        from . import memory as memory_module
        memory_module.build_day(db, turn["role_id"], memory_module._day_of(time.time()))
    except Exception:
        pass

SCHEMA = """
CREATE TABLE IF NOT EXISTS role_models(
 role_id TEXT PRIMARY KEY REFERENCES roles(id), config TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS role_turns(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
 role_id TEXT NOT NULL REFERENCES roles(id), source TEXT NOT NULL, source_id TEXT NOT NULL,
 execution_users TEXT NOT NULL, mode TEXT NOT NULL, prompt TEXT NOT NULL, payload TEXT NOT NULL,
 state TEXT NOT NULL, messages TEXT NOT NULL DEFAULT '[]', pending_task_id TEXT,
 model_steps INTEGER NOT NULL DEFAULT 0, final_text TEXT, error_code TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(source,source_id));
CREATE INDEX IF NOT EXISTS turns_by_role ON role_turns(role_id,seq,state);
CREATE TABLE IF NOT EXISTS model_calls(
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES role_turns(id), model TEXT NOT NULL,
 request TEXT NOT NULL, response TEXT, state TEXT NOT NULL, error_code TEXT,
 created_at REAL NOT NULL, finished_at REAL);
CREATE TABLE IF NOT EXISTS documents(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, content TEXT NOT NULL, core INTEGER NOT NULL,
 role_ids TEXT NOT NULL, revision INTEGER NOT NULL, deleted_at REAL);
"""


def enqueue_turn(db, role_id, source, source_id, users, mode, prompt, payload, created_at=None, caller=None):
    now = time.time() if created_at is None else created_at
    turn_id = str(uuid.uuid4())
    # ``source`` stays a coarse category (chat/trigger/scheduled/command). The
    # optional ``caller`` records WHICH surface produced it (web, feishu, weixin,
    # an alarm source, ...) so a channel can read back only its own turns' replies
    # and never sees another channel's conversation. Defaults to the source so
    # existing rows and callers keep their meaning.
    db.execute("INSERT INTO role_turns(id,role_id,source,source_id,execution_users,mode,prompt,payload,state,created_at,updated_at,caller) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (turn_id, role_id, source, source_id, json.dumps(users), mode, prompt, json.dumps(payload, ensure_ascii=False), "queued", now, now, caller or source))
    return turn_id


def migrate(db):
    for table in ("tasks", "trigger_events"):
        columns = {r[1] for r in db.execute("PRAGMA table_info(" + table + ")")}
        if "turn_id" not in columns:
            db.execute("ALTER TABLE " + table + " ADD COLUMN turn_id TEXT")
    # Schema 8: record which calling surface produced each turn so channels can
    # read back only their own conversation. Backfill old rows with the source.
    columns = {r[1] for r in db.execute("PRAGMA table_info(role_turns)")}
    if "caller" not in columns:
        db.execute("ALTER TABLE role_turns ADD COLUMN caller TEXT")
        db.execute("UPDATE role_turns SET caller=source WHERE caller IS NULL")
    # Merge legacy commands and inputs by creation time instead of putting one
    # old queue entirely ahead of the other. Stable ties are deterministic.
    rows = [(r["created_at"], "command", dict(r)) for r in db.execute("SELECT * FROM tasks WHERE turn_id IS NULL")]
    rows += [(r["created_at"], "event", dict(r)) for r in db.execute("SELECT * FROM trigger_events WHERE turn_id IS NULL")]
    for _, kind, row in sorted(rows, key=lambda item: (item[0], item[1], item[2]["id"])):
        if kind == "command":
            body = json.loads(row["payload"])
            turn_id = enqueue_turn(db, row["role_id"], "command", row["id"], body.get("execution_users", []), body.get("mode", "direct"), "Administrative command", {}, row["created_at"])
            state = {"succeeded": "completed", "failed": "failed", "cancelled": "cancelled", "unknown": "blocked_unknown"}.get(row["state"], "waiting_tool")
            db.execute("UPDATE role_turns SET state=?,pending_task_id=? WHERE id=?", (state, row["id"], turn_id))
            db.execute("UPDATE tasks SET turn_id=? WHERE id=?", (turn_id, row["id"]))
        else:
            body = json.loads(row["snapshot"])
            turn_id = enqueue_turn(db, row["role_id"], row["source"], row["id"], body["execution_users"], body["mode"], body["prompt"], json.loads(row["payload"]), row["created_at"])
            db.execute("UPDATE trigger_events SET turn_id=? WHERE id=?", (turn_id, row["id"]))


def set_turn_state(db, turn_id, state, error=None, final=None):
    db.execute("UPDATE role_turns SET state=?,error_code=?,final_text=?,updated_at=? WHERE id=?", (state, error, final, time.time(), turn_id))
    db.execute("UPDATE trigger_events SET state=? WHERE turn_id=?", (state, turn_id))


class RoleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    model: str = Field(default="", max_length=150)
    max_model_steps: int = Field(default=8, ge=1, le=1000)
    max_output_tokens: int = Field(default=2048, ge=64, le=32768)
    max_context_chars: int = Field(default=250000, ge=1000, le=2000000)


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=32000)
    execution_users: list[USER] = Field(default_factory=list, max_length=100)
    mode: Literal["readonly", "confirm", "direct"] = "confirm"
    idempotency_key: str = Field(min_length=8, max_length=128)


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: ID
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=200000)
    core: bool = True
    role_ids: list[ID] = Field(default_factory=list, max_length=100)


class ExecuteCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: ID
    run_as: USER
    command: str = Field(min_length=1, max_length=16000)
    timeout_seconds: int = Field(default=60, ge=1, le=3600)


SYSTEM_TEXT = SYSTEM

# Three operation modes, distinct in real tool authority (not just prompt text):
#   readonly - the model gets NO execute_command tool at all; it can only analyze
#              and use read-only search tools. It cannot run anything on a host.
#   confirm  - every command the model proposes enters awaiting_approval; a human
#              approves it before it is queued. Default.
#   direct   - commands are queued for execution immediately.
MODES = ("readonly", "confirm", "direct")


def tool_spec(users, search_provider=None, mode="confirm"):
    specs = []
    # readonly turns deliberately withhold the execution tool entirely. This is a
    # real capability boundary: a jailbroken prompt still has nothing to call.
    if users and mode != "readonly":
        specs.append({"type": "function", "function": {"name": "execute_command", "description": "Execute one command on a registered asset using a selected native account. The service owns authorization and confirmation; never change them.",
            "parameters": {"type": "object", "additionalProperties": False, "properties": {
                "asset_id": {"type": "string"}, "run_as": {"type": "string", "enum": users},
                "command": {"type": "string"}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600}},
                "required": ["asset_id", "run_as", "command"]}}})
    if search_provider is not None:
        from .search import search_tool_spec, fetch_tool_spec
        specs.append(search_tool_spec())
        specs.append(fetch_tool_spec())
    return specs


def tools_for_turn(turn, settings, search_provider):
    """Expose tools according to the turn's immutable mode + selected accounts."""
    users = json.loads(turn["execution_users"])
    mode = turn["mode"]
    # Defense in depth: modes are validated on entry, but never trust a stored
    # value blindly. An unknown mode must not silently become "direct".
    if mode not in MODES:
        mode = "confirm"
    return tool_spec(users, search_provider, mode) or None


class RoleEngine:
    def __init__(self, app, transaction, audit, default_model="", search_provider=None):
        self.app, self.transaction, self.audit = app, transaction, audit
        self.default_model = default_model
        self.search_provider = search_provider

    def request_body(self, db, turn, settings):
        tools = tools_for_turn(turn, settings, self.search_provider)
        return build_body(self.app, db, turn, settings, self.default_model, tools)

    def advance(self, client):
        call_id = None
        # Reserve one head-of-role turn in a transaction. Different worker threads
        # can call models for different roles, never concurrently for one role.
        with self.transaction() as db:
            rows = db.execute("""SELECT t.*,m.config AS model_config FROM role_turns t JOIN role_models m ON m.role_id=t.role_id
                WHERE t.source!='command' AND t.state IN ('queued','ready','waiting_tool')
                AND NOT EXISTS(SELECT 1 FROM role_turns p WHERE p.role_id=t.role_id AND p.seq<t.seq AND p.state NOT IN ('completed','failed','cancelled'))
                ORDER BY t.seq""").fetchall()
            chosen = None
            for row in rows:
                settings = json.loads(row["model_config"])
                if not settings["enabled"]:
                    continue
                if row["state"] == "waiting_tool":
                    task = db.execute("SELECT * FROM tasks WHERE id=?", (row["pending_task_id"],)).fetchone()
                    if task is None:
                        set_turn_state(db, row["id"], "failed", "PENDING_TASK_MISSING")
                        self.audit(db, "turn.failed", row["id"], "service", {"error": "PENDING_TASK_MISSING"})
                        continue
                    if task["state"] == "unknown":
                        set_turn_state(db, row["id"], "blocked_unknown", "EXECUTION_UNKNOWN")
                        self.audit(db, "turn.blocked", row["id"], "service", {"task_id": task["id"]})
                        continue
                    if task["state"] in ("queued", "awaiting_approval", "claimed"):
                        continue
                    if task["state"] == "cancelled":
                        set_turn_state(db, row["id"], "cancelled")
                        continue
                    messages = json.loads(row["messages"])
                    tool_id = messages[-1]["tool_calls"][0]["id"]
                    messages.append({"role": "tool", "tool_call_id": tool_id, "content": task["result"] or json.dumps({"status": task["state"]})})
                    db.execute("UPDATE role_turns SET messages=?,state='ready',pending_task_id=NULL WHERE id=?", (json.dumps(messages, ensure_ascii=False), row["id"]))
                    row = db.execute("SELECT * FROM role_turns WHERE id=?", (row["id"],)).fetchone()
                chosen = (dict(row), settings)
                break
            if chosen is None:
                return False
            turn, settings = chosen
            try:
                if turn["model_steps"] >= settings["max_model_steps"]:
                    raise ModelFailure("MODEL_STEP_BUDGET_EXHAUSTED")
                body = self.request_body(db, turn, settings)
            except ModelFailure as e:
                set_turn_state(db, turn["id"], "failed", str(e))
                self.audit(db, "turn.failed", turn["id"], "service", {"error": str(e)})
                return True
            call_id = str(uuid.uuid4())
            db.execute("INSERT INTO model_calls(id,turn_id,model,request,state,created_at) VALUES(?,?,?,?,?,?)", (call_id, turn["id"], body["model"], json.dumps(body, ensure_ascii=False), "calling", time.time()))
            db.execute("UPDATE role_turns SET state='calling',model_steps=model_steps+1,updated_at=? WHERE id=?", (time.time(), turn["id"]))
            db.execute("UPDATE trigger_events SET state='calling' WHERE turn_id=?", (turn["id"],))
            self.audit(db, "model.request", turn["id"], "service", {"call_id": call_id, "model": body["model"]})
        # Network is outside the SQLite transaction. Authorization never leaves
        # the trusted context in editable tool arguments.
        response, error = None, None
        try:
            response = client.complete(body)
        except ModelFailure as e:
            error = str(e)
        except Exception:
            error = "MODEL_CLIENT_FAILED"
        with self.transaction() as db:
            db.execute("UPDATE model_calls SET response=?,state=?,error_code=?,finished_at=? WHERE id=?",
                (json.dumps(response, ensure_ascii=False) if response else None, "failed" if error else "completed", error, time.time(), call_id))
            self.audit(db, "model.response", turn["id"], "service", {"call_id": call_id, "error": error})
            current = db.execute("SELECT * FROM role_turns WHERE id=?", (turn["id"],)).fetchone()
            if current["state"] != "calling":
                return True
            if error:
                set_turn_state(db, turn["id"], "failed", error)
                return True
            message = response["message"]
            messages = json.loads(current["messages"])
            messages.append(message)
            db.execute("UPDATE role_turns SET messages=? WHERE id=?", (json.dumps(messages, ensure_ascii=False), turn["id"]))
            calls = message.get("tool_calls") or []
            if not calls:
                set_turn_state(db, turn["id"], "completed", final=message.get("content") or "")
                self.audit(db, "turn.completed", turn["id"], "service", {})
                refresh_day_memory(db, turn)
                return True
            try:
                if len(calls) != 1:
                    raise ValueError("Unsupported tool")
                name = calls[0]["function"]["name"]
                if name not in ("execute_command", "web_search", "fetch_page"):
                    raise ValueError("Unsupported tool")
                arguments = json.loads(calls[0]["function"]["arguments"])
            except Exception:
                set_turn_state(db, turn["id"], "failed", "MODEL_TOOL_AUTHORIZATION_OR_SCHEMA_REJECTED")
                self.audit(db, "model.tool_rejected", turn["id"], "service", {"call_id": call_id})
                return True
            if name in ("web_search", "fetch_page"):
                # Read-only information tools resolved inline. No asset, no
                # account, no confirmation gate: they cannot change a host.
                # Result is untrusted data, stored as an ordinary tool message.
                tool_id = calls[0]["id"]
                if self.search_provider is None:
                    content = json.dumps({"error": "SEARCH_NOT_CONFIGURED"})
                else:
                    from .search import SearchFailure, fetch_page
                    try:
                        if name == "web_search":
                            query = str(arguments.get("query") or "")[:1000]
                            if not query:
                                raise ValueError("empty query")
                            count = arguments.get("count")
                            count = min(max(int(count), 1), 10) if isinstance(count, int) else 5
                            content = json.dumps({"results": self.search_provider.search(query, count)}, ensure_ascii=False)
                        else:
                            url = str(arguments.get("url") or "")[:2000]
                            content = json.dumps({"text": fetch_page(url)}, ensure_ascii=False)
                    except (SearchFailure, ValueError) as e:
                        content = json.dumps({"error": str(e) or "SEARCH_FAILED"})
                messages.append({"role": "tool", "tool_call_id": tool_id, "content": content})
                db.execute("UPDATE role_turns SET messages=?,state='ready' WHERE id=?", (json.dumps(messages, ensure_ascii=False), turn["id"]))
                self.audit(db, "model.search", turn["id"], "service", {"call_id": call_id, "tool": name})
                return True
            try:
                # A readonly turn must never execute. If the model emits the tool
                # anyway (it should not have it), reject rather than run. Reject
                # the turn: silently ignoring could let the model keep "trying".
                if current["mode"] == "readonly":
                    raise ValueError("Execution tool is not available in readonly mode")
                command = ExecuteCommand.model_validate(arguments)
                users = json.loads(current["execution_users"])
                if command.run_as not in users:
                    raise ValueError("Account not selected for turn")
                asset = db.execute("SELECT * FROM assets WHERE id=?", (command.asset_id,)).fetchone()
                if asset is None or command.run_as not in json.loads(asset["allowed_users"]):
                    raise ValueError("Account unavailable on asset")
            except Exception:
                set_turn_state(db, turn["id"], "failed", "MODEL_TOOL_AUTHORIZATION_OR_SCHEMA_REJECTED")
                self.audit(db, "model.tool_rejected", turn["id"], "service", {"call_id": call_id})
                return True
            task_id = str(uuid.uuid4())
            # Only the immutable turn mode decides gating. confirm -> human must
            # approve; direct -> queued now. (readonly was rejected above.)
            state = "awaiting_approval" if current["mode"] == "confirm" else "queued"
            payload = {**command.model_dump(), "role_id": current["role_id"], "execution_users": users, "mode": current["mode"], "idempotency_key": "model:" + call_id}
            db.execute("INSERT INTO tasks(id,role_id,asset_id,payload,state,idempotency_key,created_at,updated_at,turn_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (task_id, current["role_id"], command.asset_id, json.dumps(payload, ensure_ascii=False), state, payload["idempotency_key"], time.time(), time.time(), turn["id"]))
            db.execute("UPDATE role_turns SET state='waiting_tool',pending_task_id=? WHERE id=?", (task_id, turn["id"]))
            db.execute("UPDATE trigger_events SET state='waiting_tool' WHERE turn_id=?", (turn["id"],))
            self.audit(db, "task.submitted", task_id, "model-tool", {"turn_id": turn["id"], **payload})
        return True


def install_turns(app, transaction, audit, admin, default_model="", search_provider=None):
    engine = RoleEngine(app, transaction, audit, default_model, search_provider)
    app.state.role_engine = engine

    def require_role(db, role_id):
        if not db.execute("SELECT 1 FROM roles WHERE id=?", (role_id,)).fetchone():
            raise HTTPException(404, "Role not found")

    def view(row):
        return {**dict(row), "execution_users": json.loads(row["execution_users"]), "payload": json.loads(row["payload"]), "messages": json.loads(row["messages"]), "caller": row["caller"] or row["source"]}

    @app.put("/api/v1/roles/{role_id}/model", dependencies=[Depends(admin)])
    def configure_model(role_id: ID, body: RoleModel):
        with transaction() as db:
            require_role(db, role_id)
            db.execute("INSERT INTO role_models VALUES(?,?) ON CONFLICT(role_id) DO UPDATE SET config=excluded.config", (role_id, body.model_dump_json()))
            audit(db, "role.model_configured", role_id, "admin", body.model_dump())
        return body

    @app.get("/api/v1/roles/{role_id}/model", dependencies=[Depends(admin)])
    def read_model(role_id: ID):
        with transaction() as db:
            require_role(db, role_id)
            row = db.execute("SELECT config FROM role_models WHERE role_id=?", (role_id,)).fetchone()
            return json.loads(row["config"]) if row else RoleModel().model_dump()

    @app.post("/api/v1/roles/{role_id}/messages", dependencies=[Depends(admin)], status_code=202)
    def message(role_id: ID, body: Message):
        if len(set(body.execution_users)) != len(body.execution_users):
            raise HTTPException(422, "Duplicate execution users")
        source_id = role_id + ":" + body.idempotency_key
        with transaction() as db:
            require_role(db, role_id)
            old = db.execute("SELECT * FROM role_turns WHERE source='chat' AND source_id=?", (source_id,)).fetchone()
            if old:
                if old["prompt"] != body.text or json.loads(old["execution_users"]) != body.execution_users or old["mode"] != body.mode:
                    raise HTTPException(409, "Message idempotency key reused with different content")
                return {"turn_id": old["id"], "state": old["state"], "duplicate": True}
            turn_id = enqueue_turn(db, role_id, "chat", source_id, body.execution_users, body.mode, body.text, {}, caller="web")
            audit(db, "chat.received", turn_id, "admin", body.model_dump())
            return {"turn_id": turn_id, "state": "queued", "duplicate": False}

    @app.get("/api/v1/turns/{turn_id}", dependencies=[Depends(admin)])
    def get_turn(turn_id: str):
        with transaction() as db:
            row = db.execute("SELECT * FROM role_turns WHERE id=?", (turn_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Turn not found")
            return view(row)

    @app.get("/api/v1/roles/{role_id}/turns", dependencies=[Depends(admin)])
    def list_turns(role_id: ID, after: int = 0, limit: int = 100):
        if after < 0 or not 1 <= limit <= 500:
            raise HTTPException(422, "Invalid pagination")
        with transaction() as db:
            require_role(db, role_id)
            return [view(r) for r in db.execute("SELECT * FROM role_turns WHERE role_id=? AND seq>? ORDER BY seq LIMIT ?", (role_id, after, limit))]

    @app.get("/api/v1/turns/{turn_id}/model-calls", dependencies=[Depends(admin)])
    def calls(turn_id: str):
        with transaction() as db:
            return [{**dict(r), "request": json.loads(r["request"]), "response": json.loads(r["response"]) if r["response"] else None} for r in db.execute("SELECT * FROM model_calls WHERE turn_id=? ORDER BY created_at", (turn_id,))]

    @app.post("/api/v1/turns/{turn_id}/cancel", dependencies=[Depends(admin)])
    def cancel_turn(turn_id: str):
        with transaction() as db:
            row = db.execute("SELECT * FROM role_turns WHERE id=?", (turn_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Turn not found")
            if row["state"] not in ("queued", "ready", "waiting_tool"):
                raise HTTPException(409, "Cannot cancel active model call or unknown/terminal turn in this preview")
            task = db.execute("SELECT * FROM tasks WHERE id=?", (row["pending_task_id"],)).fetchone()
            if task and task['state'] == 'unknown':
                raise HTTPException(409, 'Unknown execution cannot be cleared by cancellation')
            if task and task['state'] == 'claimed':
                from .execution import cancel_task
                state = cancel_task(db, audit, task)
                return {'state': state}
            db.execute("UPDATE tasks SET state='cancelled',updated_at=? WHERE turn_id=? AND state IN ('queued','awaiting_approval')", (time.time(), turn_id))
            set_turn_state(db, turn_id, "cancelled")
            audit(db, "turn.cancelled", turn_id, "admin", {})
        return {"state": "cancelled"}

    @app.put("/api/v1/documents/{document_id}", dependencies=[Depends(admin)])
    def save_document(document_id: ID, body: Document):
        if document_id != body.id:
            raise HTTPException(422, "Document ID mismatch")
        with transaction() as db:
            for role in body.role_ids:
                require_role(db, role)
            db.execute("INSERT INTO documents VALUES(?,?,?,?,?,1,NULL) ON CONFLICT(id) DO UPDATE SET name=excluded.name,content=excluded.content,core=excluded.core,role_ids=excluded.role_ids,revision=documents.revision+1,deleted_at=NULL", (body.id, body.name, body.content, int(body.core), json.dumps(body.role_ids)))
            audit(db, "document.saved", body.id, "admin", body.model_dump())
            row = db.execute("SELECT * FROM documents WHERE id=?", (body.id,)).fetchone()
            return {**dict(row), "role_ids": json.loads(row["role_ids"])}

    @app.get("/api/v1/documents", dependencies=[Depends(admin)])
    def documents():
        with transaction() as db:
            return [{**dict(r), "role_ids": json.loads(r["role_ids"])} for r in db.execute("SELECT * FROM documents WHERE deleted_at IS NULL ORDER BY id")]

    @app.delete("/api/v1/documents/{document_id}", dependencies=[Depends(admin)])
    def delete_document(document_id: ID):
        with transaction() as db:
            if not db.execute("UPDATE documents SET deleted_at=? WHERE id=? AND deleted_at IS NULL", (time.time(), document_id)).rowcount:
                raise HTTPException(404, "Document not found")
            audit(db, "document.deleted", document_id, "admin", {})
        return {"deleted": True, "audit_retained": True}

    return engine
