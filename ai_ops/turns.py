"""Role-turn queue and model orchestration, independent of the Linux agent."""
import json
import time
import uuid
from typing import Annotated, Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from . import account_grants, actions, namelists
from .context import SYSTEM, build_body
from .model_client import ModelFailure
from .models import Identifier as ID, UserName as USER, new_id
from .questions import SCHEMA as QUESTION_SCHEMA, create_question, questions_for_turn, install_questions
TERMINAL = ("completed", "failed", "cancelled")


def derive_steps(messages):
    """Turn the raw model/tool transcript into a user-facing "分析步骤" list.

    This is DERIVED, not stored separately: the authoritative transcript stays in
    ``role_turns.messages`` (and the full request/response in ``model_calls``).
    Deriving keeps one source of truth, so a step can never drift from what the
    model actually did.

    Each returned step is one of:
    * ``{kind: "thought", text}``  — the model's own words before it acts. This is
      the "碎碎念": an intent line. We only ever surface the model's *ordinary
      assistant content*; we never request or fabricate hidden reasoning.
    * ``{kind: "command", command, run_as, asset_id, status, output_excerpt,
      exit_code, truncated}`` — an ``execute_command`` tool call, joined with the
      task result when the tool message is present in the transcript.
    * ``{kind: "search", query|url, status, preview}`` — a read-only tool call.
    * ``{kind: "tool_result", tool, status}`` — a tool result whose matching call
      was earlier in the list (kept so ordering stays honest).

    A step with no text and no tool call (an empty assistant turn) is skipped.
    """
    steps = []
    pending = {}  # tool_call_id -> index into steps, to attach the result
    for message in messages or []:
        role = message.get("role")
        if role == "assistant":
            calls = message.get("tool_calls") or []
            text = (message.get("content") or "").strip()
            if text:
                steps.append({"kind": "thought", "text": text})
            for call in calls:
                fn = (call.get("function") or {})
                name = fn.get("name")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (ValueError, TypeError):
                    args = {}
                if name == "execute_command":
                    step = {"kind": "command", "status": "proposed",
                            "command": args.get("command"), "run_as": args.get("run_as"),
                            "asset_id": args.get("asset_id"), "output_excerpt": None,
                            "exit_code": None, "truncated": False}
                elif name == "propose_change":
                    # Writes are a change set, not a command string: show the
                    # actions a human is being asked to approve (§45.5).
                    step = {"kind": "change", "status": "proposed",
                            "run_as": args.get("run_as"), "asset_id": args.get("asset_id"),
                            "actions": [a.get("action") for a in (args.get("actions") or []) if isinstance(a, dict)],
                            "reason": args.get("reason"), "output_excerpt": None,
                            "exit_code": None, "truncated": False}
                elif name == "web_search":
                    step = {"kind": "search", "status": "proposed", "query": args.get("query")}
                elif name == "fetch_page":
                    step = {"kind": "search", "status": "proposed", "url": args.get("url")}
                else:
                    step = {"kind": "tool_result", "status": "proposed", "tool": name}
                pending[call.get("id")] = len(steps)
                steps.append(step)
        elif role == "tool":
            idx = pending.pop(message.get("tool_call_id"), None)
            content = message.get("content") or ""
            if idx is None:
                # Result without a matching call in this transcript slice.
                steps.append({"kind": "tool_result", "status": "done", "tool": "tool",
                              "preview": _excerpt(content)})
                continue
            steps[idx]["status"] = "done"
            steps[idx]["output_excerpt"] = _excerpt(content)
            outcome = _parse_tool_result(content)
            if outcome is not None:
                if "exit_code" in outcome:
                    steps[idx]["exit_code"] = outcome.get("exit_code")
                if outcome.get("output_truncated"):
                    steps[idx]["truncated"] = True
                if outcome.get("status"):
                    steps[idx]["status"] = outcome["status"]
                if outcome.get("error"):
                    steps[idx]["error"] = outcome["error"]
    return steps


def _excerpt(text, limit=2000):
    if not text:
        return None
    return text if len(text) <= limit else text[:limit] + "…（已截断）"


def _parse_tool_result(content):
    """Best-effort parse of a task result string; never raises on odd content."""
    if not isinstance(content, str):
        return None
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        return data
    return None


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


SCHEMA += QUESTION_SCHEMA + account_grants.SCHEMA


def enqueue_turn(db, role_id, source, source_id, users, mode, prompt, payload, created_at=None, caller=None, grants=None):
    from .role_lifecycle import require_active
    from . import namelists, baseline
    require_active(db, role_id)
    now = time.time() if created_at is None else created_at
    turn_id = str(uuid.uuid4())
    # Mode and the name-list ids are frozen at enqueue time (§43.1, §44.3):
    # switching a mode means a new turn, and rebinding a list never rewrites
    # what an already-queued task was allowed to do.
    binding = namelists.snapshot_binding(db, mode)
    # ``source`` stays a coarse category (chat/trigger/scheduled/command). The
    # optional ``caller`` records WHICH surface produced it (web, feishu, weixin,
    # an alarm source, ...) so a channel can read back only its own turns' replies
    # and never sees another channel's conversation. Defaults to the source so
    # existing rows and callers keep their meaning.
    db.execute("INSERT INTO role_turns(id,role_id,source,source_id,execution_users,mode,prompt,payload,state,created_at,updated_at,caller,readonly_list,write_list,deny_list,baseline_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (turn_id, role_id, source, source_id, json.dumps(users), mode, prompt, json.dumps(payload, ensure_ascii=False), "queued", now, now, caller or source,
                binding['readonly_list'], binding['write_list'], binding['deny_list'], baseline.BASELINE_VERSION))
    # Legacy histories remain unmodified/unscoped and are denied at dispatch.
    if grants is not None:
        account_grants.snapshot(db, turn_id, grants)
    return turn_id


def migrate(db):
    for table in ("tasks", "trigger_events"):
        columns = {r[1] for r in db.execute("PRAGMA table_info(" + table + ")")}
        if "turn_id" not in columns:
            db.execute("ALTER TABLE " + table + " ADD COLUMN turn_id TEXT")
    # Schema 8: record which calling surface produced each turn so channels can
    # read back only their own conversation. Backfill old rows with the source.
    columns = {r[1] for r in db.execute("PRAGMA table_info(role_turns)")}
    for name in ("readonly_list", "write_list", "deny_list", "baseline_version"):
        if name not in columns:
            db.execute("ALTER TABLE role_turns ADD COLUMN " + name + " TEXT")
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
    if state in TERMINAL:
        db.execute("UPDATE turn_questions SET state='cancelled' WHERE turn_id=? AND state='pending'", (turn_id,))
        db.execute("UPDATE account_requests SET state='cancelled' WHERE turn_id=? AND state='pending'", (turn_id,))
    db.execute("UPDATE role_turns SET state=?,error_code=?,final_text=?,updated_at=? WHERE id=?", (state, error, final, time.time(), turn_id))
    db.execute("UPDATE trigger_events SET state=? WHERE turn_id=?", (state, turn_id))


class RoleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    model: str = Field(default="", max_length=150)
    provider_mode: Literal['inherit', 'independent'] = 'inherit'
    vision_enabled: bool = False
    base_url: str = Field(default='', max_length=2000)
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    clear_api_key: bool = False
    max_model_steps: int | None = Field(default=None, ge=1, le=1000)

    @field_validator('api_key')
    @classmethod
    def key_single_line(cls, value):
        if value and any(c in value.get_secret_value() for c in ('\r', '\n', '\x00')):
            raise ValueError('API Key must be a single line')
        return value
    max_output_tokens: int = Field(default=2048, ge=64, le=32768)
    max_context_chars: int = Field(default=250000, ge=1000, le=2000000)


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(default='', max_length=32000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode='after')
    def nonempty_message(self):
        if not self.text.strip() and not self.attachment_ids:
            raise ValueError('Text or an image is required')
        if len(self.attachment_ids) != len(set(self.attachment_ids)):
            raise ValueError('Duplicate attachment IDs')
        return self

    execution_users: list[USER] = Field(default_factory=list, max_length=100)
    execution_grants: list[account_grants.Grant] = Field(default_factory=list, max_length=100)
    mode: Literal["readonly", "confirm", "direct"] = "confirm"
    idempotency_key: str = Field(min_length=8, max_length=128)


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: ID = Field(default_factory=lambda: new_id('doc'))
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
# Modes are a capability boundary, not prompt advice.
MODES = ("readonly", "confirm", "direct")

# The only write tool the model has. It proposes; the server policy engine
# decides. The model is never told which mode it is in (docs §43.2).
PROPOSE_CHANGE = 'propose_change'

PROPOSE_CHANGE_SPEC = {'type': 'function', 'function': {'name': 'propose_change',
    'description': 'Propose one or more changes to run on a host. The service validates every action, '
                   'decides whether it runs immediately or needs human approval, and reports the outcome. '
                   'You cannot execute directly. Use only the listed action kinds and their exact parameters.',
    'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {
        'asset_id': {'type': 'string', 'description': 'Registered asset id.'},
        'run_as': {'type': 'string', 'description': 'Native account selected for this turn.'},
        'reason': {'type': 'string', 'description': 'One sentence on why this change is needed.'},
        'actions': {'type': 'array', 'minItems': 1, 'maxItems': 10, 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {'action': {'type': 'string', 'enum': sorted(actions.ACTIONS)}, 'params': {'type': 'object'}},
            'required': ['action', 'params']}}},
        'required': ['asset_id', 'run_as', 'actions']}}}


# Tools whose result comes from the read-only channel (gate 1). They are a
# closed set: the model picks a tool and structured parameters, and never sees
# a free-form command string. Shared with the Agent's independent check.
READONLY_TOOLS = ('read_file', 'list_dir', 'file_stat', 'disk_usage', 'memory_info',
                  'process_list', 'service_status', 'journal_logs', 'k8s_get', 'k8s_logs')

from .readonly_tools import ReadonlyRejected  # noqa: E402  (named in except below)


def _tool_output(task):
    """What the model sees of a finished task.

    Credential handles are reported as a count plus names, never as a value: the
    handle is exactly the form the model should use later (``cred:<id>``), and
    the whole point of the design is that it never has anything else to show
    (§42). The rest of the result passes through unchanged.
    """
    raw = task['result'] or json.dumps({'status': task['state']})
    try:
        body = json.loads(raw)
    except ValueError:
        return raw
    handles = body.pop('credentials', None) if isinstance(body, dict) else None
    if handles:
        body['credentials_found'] = [{'id': h.get('id'), 'name': h.get('name')} for h in handles]
        body['credential_notice'] = ('%d secret(s) found and stored on the managed host; '
                                     'reference them as cred:<id>. The value was never sent here.' % len(handles))
    return json.dumps(body, ensure_ascii=False) if isinstance(body, dict) else raw


def name_rules(db, turn):
    """Actions the turn's frozen write list allows, and their host scopes.

    The list ids were written onto the turn when it was queued, so rebinding a
    list later cannot widen an in-flight task. A turn predating name lists, or a
    deployment that never seeded them, falls back to the platform default list
    resolved live: the alternative — treating a missing list as "allow all" —
    would turn a configuration gap into an authorization hole.
    """
    list_id = turn['write_list'] if 'write_list' in turn.keys() else None
    if not list_id:
        seeded = db.execute("SELECT list_id FROM platform_defaults WHERE name='lists'").fetchone()
        if seeded:
            list_id = (json.loads(seeded['list_id']) or {}).get('write')
    if not list_id:
        return set(actions.ACTIONS), {}
    return namelists.rule_actions(db, list_id)


def _trigger_of(db, turn):
    """Resolve the trigger context that narrows what an unattended turn may do.

    A turn started by a custom task / alarm carries a trigger row with the
    action subset it was configured for (§43.5). Interactive turns have none,
    which is what separates "a human is watching" from "nobody is".
    """
    row = db.execute('SELECT source, source_id FROM role_turns WHERE id=?', (turn['id'],)).fetchone()
    if row is None or row['source'] not in ('trigger', 'alarm', 'schedule', 'command'):
        return {'source': None, 'policy': {}}
    config = db.execute(
        'SELECT trigger_actions, host_scope FROM trigger_policy WHERE source_id=?',
        (row['source_id'] or '',)).fetchone() if _table_exists(db, 'trigger_policy') else None
    asset_id = None
    if row['source'] == 'command':
        return {'source': None, 'policy': {}}
    actions_allowed = json.loads(config['trigger_actions']) if config and config['trigger_actions'] else []
    hosts = json.loads(config['host_scope']) if config and config['host_scope'] else {}
    return {'source': row['source'], 'policy': {'trigger_source': row['source'],
                                               'trigger_actions': actions_allowed,
                                               'action_hosts': hosts}}


def _table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def tool_spec(users, search_provider=None, mode="confirm"):
    from .questions import tool_spec as question_tool_spec
    from .asset_memory import tool_spec as asset_memory_tool_spec
    specs = [question_tool_spec(), asset_memory_tool_spec()]
    if mode != 'readonly':
        specs.append(account_grants.request_spec())
    else:
        # Read-only turns get the narrow readonly surface instead of the
        # execution tool. This is a real capability boundary (gate 1).
        from .readonly_tools import tool_spec as readonly_tool_spec
        specs.extend(readonly_tool_spec())
    # readonly turns deliberately withhold the execution tool entirely. This is a
    # real capability boundary: a jailbroken prompt still has nothing to call.
    if users and mode != "readonly":
        # Writes only ever go through propose_change; there is no tool that takes
        # a raw command string any more (docs §39.1).
        specs.append(PROPOSE_CHANGE_SPEC)
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
                    messages.append({"role": "tool", "tool_call_id": tool_id, "content": _tool_output(task)})
                    db.execute("UPDATE role_turns SET messages=?,state='ready',pending_task_id=NULL WHERE id=?", (json.dumps(messages, ensure_ascii=False), row["id"]))
                    row = db.execute("SELECT * FROM role_turns WHERE id=?", (row["id"],)).fetchone()
                chosen = (dict(row), settings)
                break
            if chosen is None:
                return False
            turn, settings = chosen
            try:
                if settings.get('max_model_steps') is not None and turn["model_steps"] >= settings["max_model_steps"]:
                    raise ModelFailure("MODEL_STEP_BUDGET_EXHAUSTED")
                body = self.request_body(db, turn, settings)
            except ModelFailure as e:
                set_turn_state(db, turn["id"], "failed", str(e))
                self.audit(db, "turn.failed", turn["id"], "service", {"error": str(e)})
                return True
            call_id = str(uuid.uuid4())
            from .attachments import compact_request
            db.execute("INSERT INTO model_calls(id,turn_id,model,request,state,created_at) VALUES(?,?,?,?,?,?)", (call_id, turn["id"], body["model"], json.dumps(compact_request(body), ensure_ascii=False), "calling", time.time()))
            db.execute("UPDATE role_turns SET state='calling',model_steps=model_steps+1,updated_at=? WHERE id=?", (time.time(), turn["id"]))
            db.execute("UPDATE trigger_events SET state='calling' WHERE turn_id=?", (turn["id"],))
            self.audit(db, "model.request", turn["id"], "service", {"call_id": call_id, "model": body["model"]})
        # Network is outside the SQLite transaction. Authorization never leaves
        # the trusted context in editable tool arguments.
        response, error = None, None
        try:
            selected_client = self.app.state.role_providers.client(settings, client)
            response = selected_client.complete(body)
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
                if name not in ("web_search", "fetch_page", "ask_user", "read_asset_memory", "request_execution_account", PROPOSE_CHANGE, *READONLY_TOOLS):
                    raise ValueError("Unsupported tool")
                arguments = json.loads(calls[0]["function"]["arguments"])
            except Exception:
                set_turn_state(db, turn["id"], "failed", "MODEL_TOOL_AUTHORIZATION_OR_SCHEMA_REJECTED")
                self.audit(db, "model.tool_rejected", turn["id"], "service", {"call_id": call_id})
                return True
            if name == 'read_asset_memory':
                from .asset_memory import MemoryRead, read_memory
                try:
                    request = MemoryRead.model_validate(arguments)
                    content = read_memory(db, request.asset_id, active_only=True)
                except Exception:
                    content = {'error': 'ASSET_NOT_FOUND_OR_INVALID_ID'}
                messages.append({'role': 'tool', 'tool_call_id': calls[0]['id'], 'content': json.dumps(content, ensure_ascii=False)})
                db.execute("UPDATE role_turns SET messages=?,state='ready' WHERE id=?", (json.dumps(messages, ensure_ascii=False), turn['id']))
                self.audit(db, 'asset.memory_read', turn['id'], 'model-tool', {'asset_id': arguments.get('asset_id') if isinstance(arguments, dict) else None})
                return True
            if name == 'request_execution_account':
                try:
                    request_id = account_grants.create_request(db, current, calls[0], arguments)
                except Exception:
                    set_turn_state(db, turn['id'], 'failed', 'MODEL_TOOL_AUTHORIZATION_OR_SCHEMA_REJECTED')
                    return True
                set_turn_state(db, turn['id'], 'waiting_user')
                self.audit(db, 'authority.requested', turn['id'], 'model-tool', {'request_id': request_id, **arguments})
                return True
            if name == 'ask_user':
                try:
                    question_id = create_question(db, current, calls[0], arguments)
                except Exception:
                    set_turn_state(db, turn['id'], 'failed', 'MODEL_TOOL_AUTHORIZATION_OR_SCHEMA_REJECTED')
                    self.audit(db, 'model.tool_rejected', turn['id'], 'service', {'call_id': call_id})
                    return True
                set_turn_state(db, turn['id'], 'waiting_user')
                self.audit(db, 'question.created', question_id, 'model-tool', {'turn_id': turn['id'], 'tool_call_id': calls[0]['id']})
                return True
            if name in READONLY_TOOLS:
                # Gate 1/2 (readonly channel): a tool choice becomes a
                # structured program+argv task. No shell, no free-form command.
                # Unlike confirm/direct there is no approval prompt: these are
                # provably non-mutating argv, so they can run inline like a
                # search. The Agent re-validates independently at execve.
                try:
                    if current['mode'] != 'readonly':
                        raise ValueError('readonly tool outside readonly mode')
                    from .readonly_tools import build
                    asset_id = str((arguments or {}).get('asset_id') or '')
                    run_as = str((arguments or {}).get('run_as') or '')
                    options = [k for k in (arguments or {}) if k not in ('asset_id', 'run_as')]
                    program, argv = build(name, {k: arguments.get(k) for k in options})
                    users_now = json.loads(current['execution_users'])
                    if not account_grants.permits(db, current['id'], asset_id, run_as):
                        raise ValueError('Asset account not selected for turn')
                    asset = db.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
                    if asset is None or asset['retired_at'] is not None \
                            or run_as not in json.loads(asset['allowed_users']) or run_as not in users_now:
                        raise ValueError('Account unavailable on asset')
                except (ValueError, ReadonlyRejected):
                    set_turn_state(db, turn['id'], 'failed', 'MODEL_TOOL_AUTHORIZATION_OR_SCHEMA_REJECTED')
                    self.audit(db, 'model.tool_rejected', turn['id'], 'service', {'call_id': call_id, 'tool': name})
                    return True
                task_id = str(uuid.uuid4())
                payload = {'kind': 'readonly', 'tool': name, 'program': program, 'args': argv,
                           'run_as': run_as, 'asset_id': asset_id, 'timeout_seconds': 60,
                           'execution_users': json.loads(current['execution_users']),
                           'role_id': current['role_id'], 'mode': current['mode'],
                           'idempotency_key': 'readonly:' + call_id}
                auth = account_grants.authority(db, turn['id'])
                payload['authorization_revision'] = auth['authorization_revision']
                payload['execution_grants'] = auth['execution_grants']
                db.execute('INSERT INTO tasks(id,role_id,asset_id,payload,state,idempotency_key,created_at,updated_at,turn_id) VALUES(?,?,?,?,?,?,?,?,?)',
                    (task_id, current['role_id'], asset_id, json.dumps(payload, ensure_ascii=False),
                     'queued', payload['idempotency_key'], time.time(), time.time(), turn['id']))
                db.execute("UPDATE role_turns SET state='waiting_tool',pending_task_id=? WHERE id=?", (task_id, turn['id']))
                self.audit(db, 'task.submitted', task_id, 'model-tool', {'turn_id': turn['id'], 'kind': 'readonly', 'program': program, 'args': argv})
                return True
            if name == PROPOSE_CHANGE:
                # Gate 3/4: the model proposes, the policy engine decides.
                # The change set is expanded to concrete argv here and never
                # reaches a shell unless the action itself is `shell.run`
                # (human-only, never unattended).
                try:
                    if current['mode'] not in ('confirm', 'direct'):
                        raise ValueError('change set outside a write mode')
                    # The model may not smuggle authority in extra keys; only
                    # these four are meaningful, anything else is a rejection.
                    if not isinstance(arguments, dict) or set(arguments) - {'asset_id', 'run_as', 'reason', 'actions'}:
                        raise ValueError('unexpected arguments')
                    asset_id = str((arguments or {}).get('asset_id') or '')
                    run_as = str((arguments or {}).get('run_as') or '')
                    steps = actions.normalize((arguments or {}).get('actions'))
                    users_now = json.loads(current['execution_users'])
                    if not account_grants.permits(db, current['id'], asset_id, run_as):
                        raise ValueError('Asset account not selected for turn')
                    asset = db.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
                    if asset is None or asset['retired_at'] is not None \
                            or run_as not in json.loads(asset['allowed_users']) or run_as not in users_now:
                        raise ValueError('Account unavailable on asset')
                    trigger = _trigger_of(db, current)
                except actions.ChangeRejected as error:
                    # The change set itself is malformed or uses a denied path.
                    # Report it as a refused proposal, not as a schema breach: the
                    # model should see the reason and can try a different action.
                    content = {'status': 'rejected', 'reason': str(error) or 'CHANGE_REJECTED'}
                    messages.append({'role': 'tool', 'tool_call_id': calls[0]['id'],
                                     'content': json.dumps(content, ensure_ascii=False)})
                    db.execute("UPDATE role_turns SET messages=?,state='ready' WHERE id=?",
                               (json.dumps(messages, ensure_ascii=False), turn['id']))
                    self.audit(db, 'change.rejected', turn['id'], 'policy-engine',
                               {'reason': str(error), 'asset_id': str((arguments or {}).get('asset_id') or '')})
                    return True
                except (ValueError, TypeError):
                    set_turn_state(db, turn['id'], 'failed', 'MODEL_TOOL_AUTHORIZATION_OR_SCHEMA_REJECTED')
                    self.audit(db, 'model.tool_rejected', turn['id'], 'service', {'call_id': call_id, 'tool': name})
                    return True
                # The write list frozen at turn start decides which actions
                # exist for this task at all (§44.3). Name rules can only narrow
                # what the action table already allows, never widen it.
                allowed, scope = name_rules(db, current)
                verdicts = actions.plan(current['mode'], steps, **trigger['policy'])
                codes = [reason for _, verdict, reason in verdicts if verdict == 'reject']
                for step in steps:
                    if step['action'] not in allowed:
                        codes.append('ACTION_NOT_IN_NAME_LIST')
                if scope:
                    # Host tags come from the asset, so a rule scoped to tag:prod
                    # simply does not exist on a host without that tag.
                    codes.extend(reason for _, verdict, reason in actions.plan(
                        current['mode'], steps,
                        **{**trigger['policy'], 'action_hosts': scope,
                           'asset_tags': namelists.tags_for(db, asset_id)})
                        if verdict == 'reject')
                digest = actions.payload_hash(steps, asset_id, run_as)
                if codes:
                    # A change set is atomic: one refused step refuses all, so a
                    # prompt cannot smuggle a permitted action alongside a
                    # refused one.
                    content = {'status': 'rejected', 'reason': codes[0], 'reasons': codes}
                    messages.append({'role': 'tool', 'tool_call_id': calls[0]['id'],
                                     'content': json.dumps(content, ensure_ascii=False)})
                    db.execute("UPDATE role_turns SET messages=?,state='ready' WHERE id=?",
                               (json.dumps(messages, ensure_ascii=False), turn['id']))
                    self.audit(db, 'change.rejected', turn['id'], 'policy-engine',
                               {'reasons': codes, 'asset_id': asset_id, 'actions': [s['action'] for s in steps]})
                    return True
                needs_human = any(verdict == 'approve' for _, verdict, _ in verdicts)
                task_id = str(uuid.uuid4())
                auth = account_grants.authority(db, turn['id'])
                payload = {'kind': 'change', 'asset_id': asset_id, 'run_as': run_as,
                           'execution_users': users_now, 'role_id': current['role_id'],
                           'mode': current['mode'], 'steps': steps,
                           'approval': {'payload_sha256': digest},
                           'reason': str((arguments or {}).get('reason') or '')[:1000],
                           'timeout_seconds': max(s['timeout_seconds'] for s in steps),
                           'idempotency_key': 'change:' + call_id,
                           'authorization_revision': auth['authorization_revision'],
                           'execution_grants': auth['execution_grants']}
                db.execute('INSERT INTO tasks(id,role_id,asset_id,payload,state,idempotency_key,created_at,updated_at,turn_id) VALUES(?,?,?,?,?,?,?,?,?)',
                    (task_id, current['role_id'], asset_id, json.dumps(payload, ensure_ascii=False),
                     'awaiting_approval' if needs_human else 'queued',
                     payload['idempotency_key'], time.time(), time.time(), turn['id']))
                db.execute("UPDATE role_turns SET state='waiting_tool',pending_task_id=? WHERE id=?", (task_id, turn['id']))
                self.audit(db, 'change.proposed', task_id, 'model-tool',
                           {'turn_id': turn['id'], 'payload_sha256': digest,
                            'gating': 'approval' if needs_human else 'auto',
                            'steps': [[s['action'], s['program'], s['args']] for s in steps]})
                return True
            if name in ("web_search", "fetch_page"):
                # Read-only information tools resolved inline. No asset, no
                # account, no confirmation gate: they cannot change a host.
                # Result is untrusted data, stored as an ordinary tool message.
                tool_id = calls[0]["id"]
                # Commit a durable reservation before external IO. Other workers
                # skip tool_calling; cancellation can win without waiting on IO.
                set_turn_state(db, turn['id'], 'tool_calling')
                self.audit(db, 'tool.reserved', turn['id'], 'service', {'call_id': call_id, 'tool_call_id': tool_id})
                db.commit()
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
                    except SearchFailure as error:
                        content = json.dumps({'error': str(error) or 'SEARCH_FAILED'})
                    except Exception:
                        content = json.dumps({'error': 'SEARCH_FAILED'})
                db.execute('BEGIN IMMEDIATE')
                active = db.execute('SELECT state FROM role_turns WHERE id=?', (turn['id'],)).fetchone()
                if not active or active['state'] != 'tool_calling':
                    return True
                messages.append({"role": "tool", "tool_call_id": tool_id, "content": content})
                db.execute("UPDATE role_turns SET messages=?,state='ready' WHERE id=?", (json.dumps(messages, ensure_ascii=False), turn["id"]))
                self.audit(db, "model.search", turn["id"], "service", {"call_id": call_id, "tool": name})
                return True
            try:
                # A readonly turn must never execute. If the model emits the tool
                # anyway (it should not have it), reject rather than run. Reject
                # the turn: silently ignoring could let the model keep "trying".
                if current["mode"] not in ("confirm", "direct"):
                    raise ValueError("Execution tool requires a valid execution mode")
                command = ExecuteCommand.model_validate(arguments)
                users = json.loads(current["execution_users"])
                if not account_grants.permits(db, current['id'], command.asset_id, command.run_as):
                    raise ValueError("Asset account not selected for turn")
                asset = db.execute("SELECT * FROM assets WHERE id=?", (command.asset_id,)).fetchone()
                if asset is None or asset['retired_at'] is not None or command.run_as not in json.loads(asset["allowed_users"]):
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
            payload['authorization_revision'] = account_grants.authority(db, turn['id'])['authorization_revision']
            payload['execution_grants'] = account_grants.authority(db, turn['id'])['execution_grants']
            db.execute("INSERT INTO tasks(id,role_id,asset_id,payload,state,idempotency_key,created_at,updated_at,turn_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (task_id, current["role_id"], command.asset_id, json.dumps(payload, ensure_ascii=False), state, payload["idempotency_key"], time.time(), time.time(), turn["id"]))
            db.execute("UPDATE role_turns SET state='waiting_tool',pending_task_id=? WHERE id=?", (task_id, turn["id"]))
            db.execute("UPDATE trigger_events SET state='waiting_tool' WHERE turn_id=?", (turn["id"],))
            self.audit(db, "task.submitted", task_id, "model-tool", {"turn_id": turn["id"], **payload})
        return True


def install_turns(app, transaction, audit, admin, default_model="", search_provider=None):
    engine = RoleEngine(app, transaction, audit, default_model, search_provider)
    app.state.role_engine = engine
    install_questions(app, transaction, audit, admin)
    account_grants.install(app, transaction, audit, admin)

    def require_role(db, role_id):
        if not db.execute("SELECT 1 FROM roles WHERE id=?", (role_id,)).fetchone():
            raise HTTPException(404, "Role not found")

    def view(db, row):
        data = {**dict(row), "execution_users": json.loads(row["execution_users"]), "payload": json.loads(row["payload"]), "messages": json.loads(row["messages"]), "caller": row["caller"] or row["source"]}
        data["steps"] = derive_steps(data["messages"])
        data['questions'] = questions_for_turn(db, row['id'])
        from .attachments import for_turn
        data['attachments'] = for_turn(db, row['id'])
        data.update(account_grants.authority(db, row['id']))
        data['account_requests'] = account_grants.requests(db, row['id'])
        data['authorization_events'] = [dict(r) for r in db.execute('SELECT * FROM authority_events WHERE turn_id=? ORDER BY seq', (row['id'],))]
        return data

    @app.put("/api/v1/roles/{role_id}/model", dependencies=[Depends(admin)])
    def configure_model(role_id: ID, body: RoleModel):
        providers = app.state.role_providers
        with providers.mutation() as created:
            with transaction() as db:
                require_role(db, role_id)
                previous = db.execute('SELECT config FROM role_models WHERE role_id=?', (role_id,)).fetchone()
                settings = providers.prepare(role_id, body, json.loads(previous['config']) if previous else {}, created)
                db.execute("INSERT INTO role_models VALUES(?,?) ON CONFLICT(role_id) DO UPDATE SET config=excluded.config", (role_id, json.dumps(settings)))
                audit(db, "role.model_configured", role_id, "admin", {'enabled': settings['enabled'], 'provider_mode': settings['provider_mode'], 'key_changed': bool(created) or body.clear_api_key})
        return providers.view(settings)

    @app.get("/api/v1/roles/{role_id}/model", dependencies=[Depends(admin)])
    def read_model(role_id: ID):
        with app.state.read_transaction() as db:
            require_role(db, role_id)
            row = db.execute("SELECT config FROM role_models WHERE role_id=?", (role_id,)).fetchone()
            return app.state.role_providers.view(json.loads(row["config"]) if row else RoleModel().model_dump(exclude={'api_key', 'clear_api_key'}))

    @app.post("/api/v1/roles/{role_id}/messages", dependencies=[Depends(admin)], status_code=202)
    def message(role_id: ID, body: Message):
        source_id = role_id + ":" + body.idempotency_key
        with transaction() as db:
            require_role(db, role_id)
            grants = account_grants.resolve_input(db, body.execution_grants, body.execution_users)
            from .attachments import validate_ids, bind, for_turn
            validate_ids(db, role_id, body.attachment_ids)
            old = db.execute("SELECT * FROM role_turns WHERE source='chat' AND source_id=?", (source_id,)).fetchone()
            if old:
                if old["prompt"] != body.text or account_grants.authority(db, old['id'])['initial_execution_grants'] != grants or old["mode"] != body.mode or [a['id'] for a in for_turn(db, old['id'])] != body.attachment_ids:
                    raise HTTPException(409, "Message idempotency key reused with different content")
                return {"turn_id": old["id"], "state": old["state"], "duplicate": True}
            if body.attachment_ids:
                configured = db.execute('SELECT config FROM role_models WHERE role_id=?', (role_id,)).fetchone()
                if not configured or not json.loads(configured['config']).get('vision_enabled'):
                    raise HTTPException(409, '当前角色未启用图片理解，请在模型设置确认服务商支持视觉后开启')
            # Enforce readiness for the real service (embedded engines remain usable).
            if getattr(app.state, 'enforce_model_readiness', False):
                row = db.execute('SELECT config FROM role_models WHERE role_id=?', (role_id,)).fetchone()
                settings = json.loads(row['config']) if row else {}
                from .runtime_status import model_status
                runtime = model_status(app)
                if not settings.get('enabled'):
                    raise HTTPException(409, '模型未启用：请打开角色设置启用模型；本次消息未入队')
                independent = settings.get('provider_mode') == 'independent'
                provider_ready = bool(settings.get('credential_ref')) if independent else runtime['provider_configured']
                if not runtime['worker_running'] or not provider_ready or not (settings.get('model') or default_model):
                    raise HTTPException(409, '模型服务未就绪：请检查全局服务配置；本次消息未入队')
            turn_id = enqueue_turn(db, role_id, "chat", source_id, sorted({g['username'] for g in grants}), body.mode, body.text, {}, caller="web", grants=grants)
            bind(db, turn_id, role_id, body.attachment_ids)
            audit(db, "chat.received", turn_id, "admin", body.model_dump())
            return {'turn_id': turn_id, 'state': 'queued', 'duplicate': False, 'execution_grants': grants,
                    'account_input_warning': 'Deprecated usernames narrowed once to unambiguous registered assets; unmatched names grant nothing' if body.execution_users else None}

    @app.get("/api/v1/turns/{turn_id}", dependencies=[Depends(admin)])
    def get_turn(turn_id: str):
        with app.state.read_transaction() as db:
            row = db.execute("SELECT * FROM role_turns WHERE id=?", (turn_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Turn not found")
            return view(db, row)

    @app.get("/api/v1/roles/{role_id}/turns", dependencies=[Depends(admin)])
    def list_turns(role_id: ID, after: int = 0, limit: int = 100, latest: bool = False, waiting_user: bool = False, before: int | None = None):
        if after < 0 or not 1 <= limit <= 500 or (before is not None and before <= after):
            raise HTTPException(422, "Invalid pagination")
        with app.state.read_transaction() as db:
            require_role(db, role_id)
            predicate = " AND state='waiting_user'" if waiting_user else ''
            params = [role_id, after]
            if before is not None:
                predicate += ' AND seq<?'
                params.append(before)
            order = 'DESC' if latest else 'ASC'
            rows = db.execute('SELECT * FROM role_turns WHERE role_id=? AND seq>?' + predicate + ' ORDER BY seq ' + order + ' LIMIT ?', (*params, limit)).fetchall()
            if latest:
                rows = list(reversed(rows))
            return [view(db, r) for r in rows]

    @app.get("/api/v1/turns/{turn_id}/model-calls", dependencies=[Depends(admin)])
    def calls(turn_id: str):
        with app.state.read_transaction() as db:
            return [{**dict(r), "request": json.loads(r["request"]), "response": json.loads(r["response"]) if r["response"] else None} for r in db.execute("SELECT * FROM model_calls WHERE turn_id=? ORDER BY created_at", (turn_id,))]

    @app.post("/api/v1/turns/{turn_id}/cancel", dependencies=[Depends(admin)])
    def cancel_turn(turn_id: str):
        with transaction() as db:
            row = db.execute("SELECT * FROM role_turns WHERE id=?", (turn_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Turn not found")
            if row["state"] not in ("queued", "ready", "waiting_tool", "waiting_user", "calling", "tool_calling"):
                raise HTTPException(409, "Cannot cancel unknown/terminal turn; resolve unknown execution explicitly")
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

    @app.post("/api/v1/documents", dependencies=[Depends(admin)], status_code=201)
    def create_document(body: Document):
        from .observability import INVENTORY_ID
        with transaction() as db:
            if body.id == INVENTORY_ID or db.execute('SELECT 1 FROM documents WHERE id=?', (body.id,)).fetchone():
                raise HTTPException(409, 'Document ID already exists')
            for role in body.role_ids:
                require_role(db, role)
            db.execute('INSERT INTO documents VALUES(?,?,?,?,?,1,NULL)', (body.id, body.name, body.content, int(body.core), json.dumps(body.role_ids)))
            audit(db, 'document.created', body.id, 'admin', {'name': body.name})
        return body

    @app.put("/api/v1/documents/{document_id}", dependencies=[Depends(admin)])
    def save_document(document_id: ID, body: Document):
        from .observability import INVENTORY_ID
        if document_id == INVENTORY_ID:
            raise HTTPException(409, 'System inventory is a read-only derived document')
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
        from .observability import INVENTORY_ID
        if document_id == INVENTORY_ID:
            raise HTTPException(409, 'System inventory is a read-only derived document')
        with transaction() as db:
            if not db.execute("UPDATE documents SET deleted_at=? WHERE id=? AND deleted_at IS NULL", (time.time(), document_id)).rowcount:
                raise HTTPException(404, "Document not found")
            audit(db, "document.deleted", document_id, "admin", {})
        return {"deleted": True, "audit_retained": True}

    return engine
