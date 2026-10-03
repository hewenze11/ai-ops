"""Built-in alarm log.

Requirement (user, 2026-10-02): every alarm is written to a built-in alarm log by
default; multiple alarm sources route through one custom-task trigger path and a
single configuration can be shared by several sources.

Design:
  * The alarm log is a durable, append-only table written inside the SAME
    transaction as the trigger decision. It is NOT derived from the model's
    memory and exists regardless of whether the resulting role turn succeeds,
    fails, or is later cancelled.
  * A source label identifies where an alarm came from (hostname, monitor name,
    upstream system). One custom task can therefore carry alarms from many
    sources; each row keeps its own source so they can be filtered apart.
  * The log is intentionally simple and human-readable; the raw event payload
    and the configuration snapshot are preserved for audit. Secrets in the
    payload are redacted with the shared scrubbing rules, the same way task
    results and audit details are.
"""
import json
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS alarm_log(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
 custom_task_id TEXT NOT NULL REFERENCES custom_tasks(id),
 source TEXT NOT NULL, dedupe_key TEXT NOT NULL,
 severity TEXT, title TEXT, summary TEXT,
 payload TEXT NOT NULL, event_id TEXT, turn_id TEXT,
 state TEXT NOT NULL, detail TEXT,
 received_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS alarm_by_task ON alarm_log(custom_task_id,seq);
CREATE INDEX IF NOT EXISTS alarm_by_source ON alarm_log(source,seq);
"""

MAX_SOURCE = 200
MAX_TITLE = 500
MAX_SUMMARY = 4000


def _clean(value, limit):
    if value is None:
        return None
    text = str(value)
    return text[:limit]


def summarise_payload(payload, limit=MAX_SUMMARY):
    """A short, human-readable line for the log list. Never the only record;
    the full payload is stored too (redacted)."""
    flat = json.dumps(payload, ensure_ascii=False)
    return flat[:limit]


def alarm_entry(payload, source=None):
    """Extract conventional alarm fields from an arbitrary event payload.

    Sources are not forced to use a fixed schema; if they include common keys we
    surface them, otherwise the whole payload becomes the summary.
    """
    if not isinstance(payload, dict):
        payload = {"value": payload}
    source = source or payload.get("source") or payload.get("host") or payload.get("monitor") or "unknown"
    title = payload.get("title") or payload.get("name") or payload.get("alert") or payload.get("subject")
    severity = payload.get("severity") or payload.get("level") or payload.get("priority")
    summary = payload.get("summary") or payload.get("message") or payload.get("description")
    return {"source": _clean(source, MAX_SOURCE) or "unknown",
            "severity": _clean(severity, 40),
            "title": _clean(title, MAX_TITLE),
            "summary": _clean(summary, MAX_SUMMARY) or summarise_payload(payload)}


def install_alarms(app, transaction, audit, admin):
    """Read API for the built-in alarm log. Writes happen in custom_tasks via
    ``record`` so they share the trigger transaction."""
    from fastapi import Depends, HTTPException

    @app.get("/api/v1/alarms", dependencies=[Depends(admin)])
    def list_alarms(custom_task_id: str | None = None, source: str | None = None,
                    after: int = 0, limit: int = 100):
        if not 1 <= limit <= 500 or after < 0:
            raise HTTPException(422, "Invalid pagination")
        with transaction() as db:
            rows = db.execute(
                "SELECT * FROM alarm_log WHERE seq>? AND (? IS NULL OR custom_task_id=?) "
                "AND (? IS NULL OR source=?) ORDER BY seq LIMIT ?",
                (after, custom_task_id, custom_task_id, source, source, limit)).fetchall()
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    @app.get("/api/v1/alarms/sources", dependencies=[Depends(admin)])
    def list_sources(custom_task_id: str | None = None):
        with transaction() as db:
            return [{"source": r["source"], "count": r["count"], "last_seq": r["last_seq"]} for r in db.execute(
                "SELECT source, COUNT(*) AS count, MAX(seq) AS last_seq FROM alarm_log "
                "WHERE (? IS NULL OR custom_task_id=?) GROUP BY source ORDER BY last_seq DESC",
                (custom_task_id, custom_task_id)).fetchall()]

    @app.get("/api/v1/alarms/{alarm_id}", dependencies=[Depends(admin)])
    def get_alarm(alarm_id: str):
        with transaction() as db:
            row = db.execute("SELECT * FROM alarm_log WHERE id=?", (alarm_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Alarm not found")
            return {**dict(row), "payload": json.loads(row["payload"])}


def record(db, audit, *, alarm_id, custom_task_id, source, dedupe_key, payload, event_id=None,
           turn_id=None, state="received", detail=None, severity=None, title=None, summary=None):
    """Append one alarm-log row. Caller supplies the surrounding transaction so
    the log and the trigger decision commit together (or roll back together)."""
    from .scrubbing import redact_structure
    safe = redact_structure(payload) if payload else payload
    db.execute(
        "INSERT INTO alarm_log(id,custom_task_id,source,dedupe_key,severity,title,summary,payload,event_id,turn_id,state,detail,received_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (alarm_id, custom_task_id, source or "unknown", dedupe_key, severity, title,
         summary if summary is not None else summarise_payload(safe),
         json.dumps(safe, ensure_ascii=False), event_id, turn_id, state, detail, time.time()))
