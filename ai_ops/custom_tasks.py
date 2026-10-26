"""Custom tasks: trigger templates and five-field Cron schedules.

Schedules produce durable outbox entries. Delivery goes through the SAME HTTP
trigger endpoint as external events. Prompts become role inputs, NEVER shell.
"""
from datetime import datetime
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Annotated, Literal
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from .models import new_id
from . import account_grants
from fastapi import Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator, StringConstraints, model_validator

ID = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")]
USER = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z_][a-zA-Z0-9_.-]{0,63}\$?$")]
MISFIRE_GRACE_SECONDS = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS custom_tasks(
 id TEXT PRIMARY KEY, config TEXT NOT NULL, revision INTEGER NOT NULL,
 token_hash TEXT NOT NULL, next_fire_at REAL, deleted_at REAL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS schedule_outbox(
 id TEXT PRIMARY KEY, custom_task_id TEXT NOT NULL REFERENCES custom_tasks(id),
 revision INTEGER NOT NULL, scheduled_for REAL NOT NULL, snapshot TEXT NOT NULL,
 payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at REAL NOT NULL, last_error TEXT, event_id TEXT,
 UNIQUE(custom_task_id, scheduled_for));
CREATE INDEX IF NOT EXISTS schedule_pending ON schedule_outbox(state,next_attempt_at);
CREATE TABLE IF NOT EXISTS trigger_events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
 custom_task_id TEXT NOT NULL REFERENCES custom_tasks(id), dedupe_key TEXT NOT NULL,
 source TEXT NOT NULL, scheduled_for REAL, payload TEXT NOT NULL,
 snapshot TEXT NOT NULL, revision INTEGER NOT NULL, role_id TEXT NOT NULL REFERENCES roles(id),
 state TEXT NOT NULL DEFAULT 'queued', created_at REAL NOT NULL,
 UNIQUE(custom_task_id,dedupe_key));
CREATE INDEX IF NOT EXISTS role_input_queue ON trigger_events(role_id,state,seq);
"""


def validate_cron(expression):
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("Use five fields: minute hour day-of-month month day-of-week")
    words = re.findall(r"[A-Za-z]+", expression)
    names = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC", "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}
    if any(word.upper() not in names for word in words) or re.search(r"[^0-9A-Za-z*,/\-\s]", expression):
        raise ValueError("Only standard five-field Cron syntax is supported; no seconds, year, @, ?, L, W or #")
    if not croniter.is_valid(expression):
        raise ValueError("Invalid Cron expression")
    return " ".join(fields)


def next_fire(expression, timezone, after):
    zone = ZoneInfo(timezone)
    iterator = croniter(expression, datetime.fromtimestamp(after, zone), day_or=True, max_years_between_matches=8)
    # Round-tripping through UTC detects imaginary local times. Skip the second
    # occurrence of repeated wall times during a fall-back DST transition.
    for _ in range(100):
        candidate = iterator.get_next(datetime).timestamp()
        local = datetime.fromtimestamp(candidate, zone)
        if candidate > after and local.fold == 0 and croniter.match(expression, local.replace(tzinfo=None), day_or=True):
            return candidate
    raise ValueError("Could not determine a valid next occurrence")


def trigger_path(custom_id):
    """Single source for the trigger URL so the console and core agree."""
    return "/api/v1/triggers/" + custom_id + "/invoke"


class TaskNotification(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    url: str | None = Field(default=None, max_length=2000)
    channel: Literal['auto', 'feishu', 'dingtalk', 'wecom', 'slack', 'discord', 'generic'] = 'auto'
    console_url: str | None = Field(default=None, max_length=2000)


def prepare_notification(body, before=None):
    """Preserve write-only secrets on normal edits; validate only on save/send."""
    from .alert_notify import validate_url, validate_console_url, redact_url
    if before is not None and 'notification' not in body.model_fields_set:
        stored = before.get('notification')
        # Empty objects are disabled overrides, not legacy inheritance.
        body.notification = TaskNotification.model_validate(stored) if stored is not None else None
    n = body.notification
    if n is None:
        return
    old = (before or {}).get('notification') or {}
    if old.get('url') and (n.url is None or n.url == redact_url(old['url'])):
        n.url = old['url']
    if n.enabled and not n.url:
        raise HTTPException(422, 'Enabled notification requires a webhook URL')
    try:
        if n.url:
            validate_url(n.url)
        if n.console_url:
            validate_console_url(n.console_url)
    except ValueError as e:
        raise HTTPException(422, str(e))


def safe_task_config(config):
    from .alert_notify import redact_url
    cfg = json.loads(json.dumps(config))
    if cfg.get('notification') and cfg['notification'].get('url'):
        cfg['notification']['url'] = redact_url(cfg['notification']['url'])
    return cfg


class CustomTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: ID = Field(default_factory=lambda: new_id('task'))
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["trigger", "scheduled"]
    role_id: ID
    prompt: str = Field(min_length=1, max_length=32000)
    execution_users: list[USER] = Field(default_factory=list, max_length=100)
    execution_grants: list[account_grants.Grant] = Field(default_factory=list, max_length=100)
    # Any registered mode id, including a derived one. An unattended task pins its
    # mode, so the id must resolve; a deleted mode is refused at delete time.
    mode: str = Field(default="confirm", min_length=1, max_length=200)
    # An unattended task never relies on inheritance (§44.3). A trigger/schedule
    # that writes must say so explicitly; a default would mean a write-capable
    # task is created by someone who never saw a mode picker. The check lives in
    # the route, where the request body's keys are still visible.
    mode_explicit: bool = Field(default=False, exclude=True, description='Set by the API when the caller supplied a mode')
    enabled: bool = True
    cron: str | None = Field(default=None, max_length=200)
    timezone: str = Field(default="Asia/Shanghai", max_length=100)
    # A newly created task never silently inherits a global delivery route.
    # Explicit null remains the legacy API's opt-in inheritance representation.
    # prepare_notification preserves the stored value on PUT omission.
    notification: TaskNotification | None = Field(default_factory=TaskNotification)

    @model_validator(mode="after")
    def check(self):
        if len(set(self.execution_users)) != len(self.execution_users):
            raise ValueError("Duplicate execution users")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError("Unknown IANA timezone") from e
        if self.kind == "scheduled":
            if not self.cron:
                raise ValueError("Scheduled tasks require cron")
            self.cron = validate_cron(self.cron)
            try:
                next_fire(self.cron, self.timezone, time.time())
            except Exception as e:
                raise ValueError("Cron has no supported next occurrence") from e
        elif self.cron is not None:
            raise ValueError("Trigger-only tasks must not specify cron")
        return self


class CronPreview(BaseModel):
    cron: str = Field(min_length=1, max_length=200)
    timezone: str = Field(default="Asia/Shanghai", max_length=100)
    count: int = Field(default=5, ge=1, le=20)


class CustomTaskStore:
    def __init__(self, transaction, audit, admin_token_provider):
        self.transaction = transaction
        self.audit = audit
        # A callable returning the current admin token, so a rotated credential
        # is honoured without restarting the service.
        self.admin_token_provider = admin_token_provider
        if isinstance(admin_token_provider, str):
            self.admin_token_provider = lambda: admin_token_provider

    @staticmethod
    def view(row):
        config = json.loads(row["config"])
        notification = config.get('notification')
        mode = ('legacy_inherit' if notification is None else
                'task_webhook' if notification.get('enabled') else 'disabled')
        return {**safe_task_config(config), "revision": row["revision"], "next_fire_at": row["next_fire_at"],
                "trigger_path": trigger_path(row["id"]), "trigger_token_active": bool(row['token_hash']), "notification_mode": mode,
                'account_input_warning': 'Deprecated usernames are frozen to execution_grants; unmatched usernames grant nothing' if config.get('execution_users') else None}

    def materialize(self, now=None):
        now = time.time() if now is None else now
        created = 0
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM custom_tasks WHERE deleted_at IS NULL AND next_fire_at<=? ORDER BY next_fire_at LIMIT 100", (now,)).fetchall()
            for row in rows:
                cfg = json.loads(row["config"])
                if not cfg["enabled"] or cfg["kind"] != "scheduled":
                    continue
                due = row["next_fire_at"]
                # No unlimited backfill after downtime. Persisted outbox delivery
                # still retries; old, never-materialized wall-clock slots are skipped.
                if now - due > MISFIRE_GRACE_SECONDS:
                    future = next_fire(cfg["cron"], cfg["timezone"], now)
                    db.execute("UPDATE custom_tasks SET next_fire_at=? WHERE id=?", (future, row["id"]))
                    self.audit(db, "schedule.missed_skipped", row["id"], "scheduler", {"first_missed_at": due, "resumed_at": now, "next_fire_at": future})
                    continue
                event = str(uuid.uuid4())
                payload = {"scheduled_for": datetime.fromtimestamp(due, ZoneInfo(cfg["timezone"])).isoformat(), "timezone": cfg["timezone"]}
                db.execute("INSERT OR IGNORE INTO schedule_outbox(id,custom_task_id,revision,scheduled_for,snapshot,payload,next_attempt_at) VALUES(?,?,?,?,?,?,?)",
                           (event, row["id"], row["revision"], due, row["config"], json.dumps(payload), now))
                db.execute("UPDATE custom_tasks SET next_fire_at=? WHERE id=?", (next_fire(cfg["cron"], cfg["timezone"], due), row["id"]))
                self.audit(db, "schedule.materialized", row["id"], "scheduler", {"outbox_id": event, "scheduled_for": due})
                created += 1
        return created

    def deliver_pending(self, sender, now=None):
        now = time.time() if now is None else now
        with self.transaction() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM schedule_outbox WHERE state='pending' AND next_attempt_at<=? ORDER BY scheduled_for LIMIT 50", (now,))]
        delivered = 0
        for row in rows:
            # HTTP outside the database transaction. Stable outbox ID makes
            # retries and concurrent scheduler processes deduplicate at ingestion.
            try:
                answer = sender(row["custom_task_id"], row["id"], json.loads(row["payload"]))
                if answer.get("accepted") is not True or not answer.get("event_id"):
                    raise RuntimeError("Trigger did not acknowledge the input")
                error = None
            except Exception as e:
                error = type(e).__name__
            with self.transaction() as db:
                current = db.execute("SELECT * FROM schedule_outbox WHERE id=?", (row["id"],)).fetchone()
                if current["state"] != "pending":
                    continue
                if error is None:
                    db.execute("UPDATE schedule_outbox SET state='delivered',event_id=?,attempts=attempts+1,last_error=NULL WHERE id=?", (answer["event_id"], row["id"]))
                    self.audit(db, "schedule.delivered", row["custom_task_id"], "scheduler", {"outbox_id": row["id"], "event_id": answer["event_id"]})
                    delivered += 1
                else:
                    attempts = current["attempts"] + 1
                    backoff = min(300, 2 ** min(attempts, 8))
                    db.execute("UPDATE schedule_outbox SET attempts=?,last_error=?,next_attempt_at=? WHERE id=?", (attempts, error, now + backoff, row["id"]))
                    self.audit(db, "schedule.delivery_failed", row["custom_task_id"], "scheduler", {"outbox_id": row["id"], "error_type": error, "attempt": attempts})
        return delivered


def install_custom_tasks(app, transaction, audit, admin, admin_token):
    store = CustomTaskStore(transaction, audit, admin_token)
    app.state.custom_tasks = store

    def require_role(db, role_id):
        from .role_lifecycle import require_active
        require_active(db, role_id)

    @app.post("/api/v1/custom-tasks/cron-preview", dependencies=[Depends(admin)])
    def preview(body: CronPreview):
        try:
            expression = validate_cron(body.cron)
            zone = ZoneInfo(body.timezone)
            when = time.time()
            occurrences = []
            for _ in range(body.count):
                when = next_fire(expression, body.timezone, when)
                occurrences.append({"timestamp": when, "local_time": datetime.fromtimestamp(when, zone).isoformat()})
            return {"cron": expression, "timezone": body.timezone, "occurrences": occurrences,
                    "day_match": "day-of-month OR day-of-week when both are restricted", "misfire_policy": "skip_unmaterialized_older_than_60_seconds"}
        except Exception as e:
            raise HTTPException(422, "Invalid Cron expression, timezone, or no supported occurrence") from e

    @app.post("/api/v1/custom-tasks", dependencies=[Depends(admin)], status_code=201)
    def create(body: CustomTask):
        body.mode_explicit = 'mode' in body.model_fields_set
        if not body.mode_explicit:
            raise HTTPException(422, 'mode is required: an unattended task never inherits one')
        prepare_notification(body)
        secret = secrets.token_urlsafe(36)
        now = time.time()
        due = next_fire(body.cron, body.timezone, now) if body.kind == "scheduled" and body.enabled else None
        with transaction() as db:
            require_role(db, body.role_id)
            body.execution_grants = [account_grants.Grant(**g) for g in account_grants.resolve_input(db, body.execution_grants, body.execution_users)]
            if db.execute("SELECT 1 FROM custom_tasks WHERE id=?", (body.id,)).fetchone():
                raise HTTPException(409, "Custom task ID already exists, including deleted records")
            db.execute("INSERT INTO custom_tasks VALUES(?,?,?,?,?,?,?,?)", (body.id, body.model_dump_json(), 1, hashlib.sha256(secret.encode()).hexdigest(), due, None, now, now))
            audit(db, "custom_task.created", body.id, "admin", body.model_dump())
            result = store.view(db.execute("SELECT * FROM custom_tasks WHERE id=?", (body.id,)).fetchone())
        return {**result, "trigger_token": secret}

    @app.get("/api/v1/custom-tasks", dependencies=[Depends(admin)])
    def listing():
        with transaction() as db:
            return [store.view(r) for r in db.execute("SELECT * FROM custom_tasks WHERE deleted_at IS NULL ORDER BY created_at")]

    @app.get("/api/v1/custom-tasks/{custom_id}", dependencies=[Depends(admin)])
    def get(custom_id: ID):
        with transaction() as db:
            row = db.execute("SELECT * FROM custom_tasks WHERE id=? AND deleted_at IS NULL", (custom_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Custom task not found")
            return store.view(row)

    @app.put("/api/v1/custom-tasks/{custom_id}", dependencies=[Depends(admin)])
    def edit(custom_id: ID, body: CustomTask):
        if custom_id != body.id:
            raise HTTPException(422, "ID must match path")
        body.mode_explicit = 'mode' in body.model_fields_set
        if not body.mode_explicit:
            raise HTTPException(422, 'mode is required: an unattended task never inherits one')
        now = time.time()
        with transaction() as db:
            require_role(db, body.role_id)
            old = db.execute("SELECT * FROM custom_tasks WHERE id=? AND deleted_at IS NULL", (custom_id,)).fetchone()
            if old is None:
                raise HTTPException(404, "Custom task not found")
            before = json.loads(old["config"])
            body.execution_grants = [account_grants.Grant(**g) for g in account_grants.resolve_input(db, body.execution_grants, body.execution_users)]
            prepare_notification(body, before)
            schedule_keys = ("kind", "cron", "timezone", "enabled")
            if all(before[k] == body.model_dump()[k] for k in schedule_keys):
                due = old["next_fire_at"]
            else:
                due = next_fire(body.cron, body.timezone, now) if body.kind == "scheduled" and body.enabled else None
            db.execute("UPDATE custom_tasks SET config=?,revision=revision+1,next_fire_at=?,updated_at=? WHERE id=?", (body.model_dump_json(), due, now, custom_id))
            if not body.enabled:
                db.execute("UPDATE schedule_outbox SET state='cancelled',last_error='CUSTOM_TASK_DISABLED' WHERE custom_task_id=? AND state='pending'", (custom_id,))
            audit(db, "custom_task.updated", custom_id, "admin", {"before": before, "after": body.model_dump()})
            return store.view(db.execute("SELECT * FROM custom_tasks WHERE id=?", (custom_id,)).fetchone())

    @app.post('/api/v1/custom-tasks/{custom_id}/trigger-token', dependencies=[Depends(admin)])
    def issue_trigger_token(custom_id: ID):
        secret = secrets.token_urlsafe(36)
        with transaction() as db:
            if not db.execute('UPDATE custom_tasks SET token_hash=?,updated_at=? WHERE id=? AND deleted_at IS NULL',
                    (hashlib.sha256(secret.encode()).hexdigest(), time.time(), custom_id)).rowcount:
                raise HTTPException(404, 'Custom task not found')
            audit(db, 'custom_task.token_rotated', custom_id, 'admin', {})
        return {'trigger_token': secret, 'trigger_token_active': True}

    @app.delete('/api/v1/custom-tasks/{custom_id}/trigger-token', dependencies=[Depends(admin)])
    def revoke_trigger_token(custom_id: ID):
        with transaction() as db:
            if not db.execute("UPDATE custom_tasks SET token_hash='',updated_at=? WHERE id=? AND deleted_at IS NULL", (time.time(), custom_id)).rowcount:
                raise HTTPException(404, 'Custom task not found')
            audit(db, 'custom_task.token_revoked', custom_id, 'admin', {})
        return {'trigger_token_active': False, 'accepted_work_unchanged': True, 'schedule_unchanged': True}

    @app.delete("/api/v1/custom-tasks/{custom_id}", dependencies=[Depends(admin)])
    def delete(custom_id: ID):
        with transaction() as db:
            count = db.execute("UPDATE custom_tasks SET deleted_at=?,next_fire_at=NULL WHERE id=? AND deleted_at IS NULL", (time.time(), custom_id)).rowcount
            if not count:
                raise HTTPException(404, "Custom task not found")
            db.execute("UPDATE schedule_outbox SET state='cancelled',last_error='CUSTOM_TASK_DELETED' WHERE custom_task_id=? AND state='pending'", (custom_id,))
            audit(db, "custom_task.deleted", custom_id, "admin", {})
        return {"deleted": True, "history_retained": True}

    @app.post("/api/v1/triggers/{custom_id}/invoke", status_code=202)
    async def invoke(custom_id: ID, request: Request, authorization: str | None = Header(default=None),
                     x_event_id: str | None = Header(default=None), x_schedule_event_id: str | None = Header(default=None),
                     x_alarm_source: str | None = Header(default=None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer authentication required")
        supplied = authorization[7:]
        internal = bool(x_schedule_event_id) and hmac.compare_digest(supplied.encode(), store.admin_token_provider().encode())
        # Authenticate before reading an attacker-controlled request body.
        with transaction() as db:
            row = db.execute("SELECT * FROM custom_tasks WHERE id=?", (custom_id,)).fetchone()
            if row is None or (not internal and not hmac.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(), row["token_hash"])):
                raise HTTPException(403, "Invalid trigger credential")
        if x_schedule_event_id and not internal:
            raise HTTPException(403, "Schedule identity is reserved to the service")
        # An optional source label lets one configuration carry alarms from many
        # sources; it is untrusted metadata, only used for the alarm log, never as
        # an instruction or authorization.
        if x_alarm_source and (len(x_alarm_source) > 200 or any(ord(c) < 32 for c in x_alarm_source)):
            raise HTTPException(422, "Invalid alarm source label")
        if x_event_id and (not 1 <= len(x_event_id) <= 128 or any(ord(c) < 32 for c in x_event_id)):
            raise HTTPException(422, "Invalid event ID")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 262144:
                raise HTTPException(413, "Trigger payload exceeds 256KiB preview limit")
        try:
            payload = json.loads(raw) if raw else {}
            if not isinstance(payload, dict):
                raise ValueError("JSON object required")
        except (ValueError, UnicodeDecodeError) as e:
            raise HTTPException(422, "Trigger body must be a JSON object") from e
        rejection = None
        answer = None
        with transaction() as db:
            row = db.execute("SELECT * FROM custom_tasks WHERE id=?", (custom_id,)).fetchone()
            # Revalidate after body read: token revocation/rotation must win the race.
            if row is None or (not internal and (not row['token_hash'] or not hmac.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(), row['token_hash']))):
                raise HTTPException(403, 'Invalid trigger credential')
            cfg = json.loads(row["config"])
            from . import alarms
            from .turns import enqueue_turn
            entry = alarms.alarm_entry(payload, x_alarm_source)
            scheduled_for = None
            revision = row["revision"]
            snapshot = row["config"]
            source = "trigger"
            key = "external:" + (x_event_id or str(uuid.uuid4()))
            if x_schedule_event_id:
                item = db.execute("SELECT * FROM schedule_outbox WHERE id=? AND custom_task_id=?", (x_schedule_event_id, custom_id)).fetchone()
                if item is None:
                    raise HTTPException(404, "Scheduled occurrence not found")
                if json.loads(item["payload"]) != payload:
                    raise HTTPException(409, "Scheduled payload does not match durable outbox")
                key = "schedule:" + x_schedule_event_id
                source = "scheduled"
                scheduled_for, snapshot, revision = item["scheduled_for"], item["snapshot"], item["revision"]
            existing = db.execute("SELECT * FROM trigger_events WHERE custom_task_id=? AND dedupe_key=?", (custom_id, key)).fetchone()
            if existing:
                if json.loads(existing["payload"]) != payload:
                    raise HTTPException(409, "Event ID reused with a different payload")
                # A duplicate is still logged, but marked as a duplicate rather
                # than silently dropped: the alarm log is the default record of
                # every alarm, including repeats.
                alarms.record(db, audit, alarm_id=str(uuid.uuid4()), custom_task_id=custom_id,
                              source=entry["source"], dedupe_key=key, payload=payload,
                              event_id=existing["id"], turn_id=existing["turn_id"],
                              state="duplicate", detail="duplicate_event", severity=entry["severity"],
                              title=entry["title"], summary=entry["summary"])
                answer = {"accepted": True, "event_id": existing["id"], "turn_id": existing["turn_id"], "state": existing["state"], "duplicate": True}
            elif row["deleted_at"] is not None or not cfg["enabled"] or (x_schedule_event_id and item["state"] == "cancelled"):
                alarms.record(db, audit, alarm_id=str(uuid.uuid4()), custom_task_id=custom_id,
                              source=entry["source"], dedupe_key=key, payload=payload,
                              state="rejected", detail="disabled_or_deleted", severity=entry["severity"],
                              title=entry["title"], summary=entry["summary"])
                audit(db, "trigger.rejected", custom_id, "scheduler" if internal else "trigger", {"reason": "disabled_or_deleted", "payload": payload})
                rejection = HTTPException(409, "Custom task disabled or deleted")
            else:
                template = json.loads(snapshot)
                event_id = str(uuid.uuid4())
                db.execute("INSERT INTO trigger_events(id,custom_task_id,dedupe_key,source,scheduled_for,payload,snapshot,revision,role_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (event_id, custom_id, key, source, scheduled_for, json.dumps(payload, ensure_ascii=False), snapshot, revision, template["role_id"], time.time()))
                grants = account_grants.validate_grants(db, template.get('execution_grants', []))
                # Legacy templates carry no asset-scoped authority. Keep accepted
                # history, but fail closed for legacy unattended execution.
                if template.get('execution_users') and 'execution_grants' not in template:
                    raise HTTPException(409, 'Legacy task requires asset-scoped account selection')
                turn_id = enqueue_turn(db, template['role_id'], source, event_id, sorted({g['username'] for g in grants}), template['mode'], template['prompt'], payload, grants=grants)
                db.execute("UPDATE trigger_events SET turn_id=? WHERE id=?", (turn_id, event_id))
                alarms.record(db, audit, alarm_id=str(uuid.uuid4()), custom_task_id=custom_id,
                              source=entry["source"], dedupe_key=key, payload=payload,
                              event_id=event_id, turn_id=turn_id, state="accepted", severity=entry["severity"],
                              title=entry["title"], summary=entry["summary"])
                audit(db, "trigger.accepted", event_id, source, {"custom_task_id": custom_id, "role_id": template["role_id"], "revision": revision, "source": source, "turn_id": turn_id})
                answer = {"accepted": True, "event_id": event_id, "turn_id": turn_id, "state": "queued", "duplicate": False}
        if rejection:
            raise rejection
        return answer

    @app.get("/api/v1/custom-task-events", dependencies=[Depends(admin)])
    def events(role_id: str | None = None, after: int = 0, limit: int = 100):
        if not 1 <= limit <= 500 or after < 0:
            raise HTTPException(422, "Invalid pagination")
        with transaction() as db:
            rows = db.execute("SELECT * FROM trigger_events WHERE seq>? AND (? IS NULL OR role_id=?) ORDER BY seq LIMIT ?", (after, role_id, role_id, limit))
            from .scrubbing import redact_structure
            return [{**dict(r), "payload": redact_structure(json.loads(r["payload"])), "snapshot": safe_task_config(json.loads(r["snapshot"]))} for r in rows]

    @app.get("/api/v1/schedule-deliveries", dependencies=[Depends(admin)])
    def deliveries(limit: int = 100):
        if not 1 <= limit <= 500:
            raise HTTPException(422, "Invalid limit")
        with transaction() as db:
            return [{k: r[k] for k in ("id", "custom_task_id", "revision", "scheduled_for", "state", "attempts", "last_error", "event_id")} for r in db.execute("SELECT * FROM schedule_outbox ORDER BY scheduled_for DESC LIMIT ?", (limit,))]

    return store
