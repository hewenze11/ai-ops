"""Alert result fan-out (outbound webhook push).

Design (docs/alert-webhook.md, 2026-10-04): Alertmanager is the FIRST hop and
owns real-time delivery, de-noising and grouping. AI Ops is the SECOND hop: it
takes an alarm, lets the role investigate, and pushes the *conclusion* (not the
raw alarm) to a user-configured webhook. We never replace Alertmanager and we
never race it to be first.

Boundaries that are deliberate
------------------------------
* **One feature, all channels.** Feishu / DingTalk / WeCom / Slack / Discord and
  a generic shape are all "POST a JSON body to a URL". We adapt the body by
  matching the URL host; we never ship a channel SDK. A channel changing its API
  is the user's concern, not ours.
* **The push content is the role's final summary**, associated by turn id. We do
  not invent a conclusion for a still-queued or failed turn.
* **SSRF is a safety floor, not a feature.** The user supplies a URL and our
  server fetches it; we refuse loopback / private / link-local / metadata
  targets and non-http(s) schemes, and re-check after DNS resolution. Without
  this, a webhook of ``http://169.254.169.254/`` would leak cloud credentials.
* **Best-effort, but never silently lost.** Deliveries are durable rows; a
  background worker retries with bounded backoff and records the error *type*,
  never the URL's secret query, never a credential.
"""
import ipaddress
import json
import socket
import time
import urllib.parse
import urllib.request
import uuid

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .models import Identifier as ID

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_notify_config(
 id INTEGER PRIMARY KEY CHECK(id=1),
 url TEXT NOT NULL, channel TEXT NOT NULL, enabled INTEGER NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS alert_notifications(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
 turn_id TEXT, dedupe_key TEXT NOT NULL, url TEXT NOT NULL, channel TEXT NOT NULL,
 title TEXT, body TEXT NOT NULL, alarm_state TEXT, status TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL,
 last_error TEXT, created_at REAL NOT NULL, delivered_at REAL,
 UNIQUE(dedupe_key));
CREATE INDEX IF NOT EXISTS alert_notify_pending ON alert_notifications(status,next_attempt_at);
"""

# A turn that finished a conclusion for an alarm is picked up here and turned
# into one push row. We join alarm_log -> role_turns on turn_id; a turn is ready
# to fan out when it is terminal AND no notification row exists for it yet.
COLLECT_SQL = """
SELECT a.turn_id AS turn_id,
       MAX(a.dedupe_key) AS dedupe_key,
       MAX(a.source) AS source,
       MAX(a.title) AS title,
       MAX(a.severity) AS severity,
       t.state AS turn_state,
       t.final_text AS final_text,
       t.role_id AS role_id
FROM alarm_log a
JOIN role_turns t ON t.id = a.turn_id
WHERE a.state='accepted' AND a.turn_id IS NOT NULL
  AND t.state IN ('completed','failed','cancelled','blocked_unknown')
  AND NOT EXISTS(SELECT 1 FROM alert_notifications n WHERE n.turn_id=a.turn_id)
GROUP BY a.turn_id
LIMIT 50
"""

MAX_URL = 2000
MAX_BODY = 16000
MAX_RESPONSE = 4096
TIMEOUT = 5.0
MAX_ATTEMPTS = 5

# Host substring -> channel. Matched in order; unknown hosts fall back to generic.
CHANNEL_HOSTS = (
    ("feishu", ("open.feishu.cn", "open.larksuite.com", "larksuite.com", "feishu.cn")),
    ("dingtalk", ("oapi.dingtalk.com", "dingtalk.com")),
    ("wecom", ("qyapi.weixin.qq.com", "qyapi.weixin.qq")),
    ("slack", ("hooks.slack.com",)),
    ("discord", ("discord.com", "discordapp.com")),
)
CHANNELS = ("auto", "feishu", "dingtalk", "wecom", "slack", "discord", "generic")


class NotifyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=MAX_URL)
    channel: str = Field(default="auto", max_length=32)
    enabled: bool = True


def classify_channel(url):
    """Return the channel for a URL by host match; never guess past the table."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    for name, suffixes in CHANNEL_HOSTS:
        if any(host == s or host.endswith("." + s) for s in suffixes):
            return name
    return "generic"


def _host_is_blocked(host):
    """True if the host resolves to (or is) a non-public address."""
    if not host:
        return True
    candidates = {host}
    try:
        candidates.add(str(ipaddress.ip_address(host)))
    except ValueError:
        pass
    # Resolve DNS ourselves so a name pointing at an internal IP is still caught.
    try:
        for info in socket.getaddrinfo(host, None):
            candidates.add(info[4][0])
    except (socket.gaierror, UnicodeError, OSError):
        # An unresolvable host is not deliverable; treat as blocked so we never
        # store a URL that only becomes dangerous after a name change.
        return True
    for cand in candidates:
        try:
            ip = ipaddress.ip_address(cand)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            return True
        # An IPv4-mapped IPv6 like ::ffff:10.0.0.1 must be unwrapped and rechecked.
        if ip.version == 6 and ip.ipv4_mapped is not None:
            inner = ip.ipv4_mapped
            if (inner.is_private or inner.is_loopback or inner.is_link_local
                    or inner.is_reserved or inner.is_unspecified):
                return True
    return False


def validate_url(url):
    """Raise ValueError with a stable reason if the URL is not a safe http(s) target."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("SSRF_UNSUPPORTED_SCHEME")
    if parts.username or parts.password:
        raise ValueError("SSRF_EMBEDDED_CREDENTIALS")
    if not parts.hostname:
        raise ValueError("SSRF_MISSING_HOST")
    if _host_is_blocked(parts.hostname):
        raise ValueError("SSRF_BLOCKED_ADDRESS")
    return parts


def redact_url(url):
    """Mask credential-looking query values so a stored URL can be shown back."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    masked = []
    secret_hint = ("key", "token", "secret", "access", "sign", "auth", "pass")
    for k, v in pairs:
        if any(h in k.lower() for h in secret_hint):
            masked.append((k, "[REDACTED]"))
        else:
            masked.append((k, v))
    query = "&".join("%s=%s" % (urllib.parse.quote(k, safe=""), urllib.parse.quote(v, safe="[]"))
                      for k, v in masked)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def build_payload(channel, title, body, url=None):
    """Adapt the push body to the channel's expected JSON shape."""
    if channel == "feishu":
        text = title + "\n" + body
        return {"msg_type": "text", "content": {"text": text}}
    if channel == "dingtalk":
        return {"msgtype": "markdown", "markdown": {"title": title, "text": "### " + title + "\n\n" + body}}
    if channel == "wecom":
        return {"msgtype": "markdown", "markdown": {"content": "**" + title + "**\n\n" + body}}
    if channel == "slack":
        return {"text": "*" + title + "*\n" + body}
    if channel == "discord":
        return {"content": "**" + title + "**\n" + body}
    return {"source": "ai-ops", "title": title, "text": title + "\n" + body, "body": body}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect could point at an internal address we already refused;
        # never follow one with a credential-bearing push.
        raise RuntimeError("SSRF_REDIRECT_REFUSED")


def post_once(url, channel, title, body):
    """Single POST attempt. Returns None on success or raises RuntimeError(type)."""
    validate_url(url)
    payload = build_payload(channel, title, body, url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "ai-ops/0.1"})
    with opener.open(req, timeout=TIMEOUT) as response:
        response.read(MAX_RESPONSE)  # bound the read; body is not interpreted


def install_alert_notify(app, transaction, audit, admin):
    """Config + read APIs. Delivery is driven by the background worker below."""

    @app.put("/api/v1/alert-notify/config", dependencies=[Depends(admin)])
    def set_config(body: NotifyConfig):
        if body.channel not in CHANNELS:
            raise HTTPException(422, "Unknown channel")
        try:
            validate_url(body.url)
        except ValueError as e:
            raise HTTPException(422, str(e))
        channel = classify_channel(body.url) if body.channel == "auto" else body.channel
        now = time.time()
        with transaction() as db:
            db.execute("INSERT INTO alert_notify_config(id,url,channel,enabled,created_at,updated_at) "
                       "VALUES(1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET url=excluded.url,"
                       "channel=excluded.channel,enabled=excluded.enabled,updated_at=excluded.updated_at",
                       (body.url, channel, 1 if body.enabled else 0, now, now))
            audit(db, "alert_notify.configured", "alert-notify", "admin",
                  {"channel": channel, "enabled": body.enabled, "url": redact_url(body.url)})
        return {"channel": channel, "enabled": body.enabled, "url": redact_url(body.url)}

    @app.get("/api/v1/alert-notify/config", dependencies=[Depends(admin)])
    def get_config():
        with transaction() as db:
            row = db.execute("SELECT * FROM alert_notify_config WHERE id=1").fetchone()
            if row is None:
                return {"configured": False}
            return {"configured": True, "channel": row["channel"], "enabled": bool(row["enabled"]),
                    "url": redact_url(row["url"]), "updated_at": row["updated_at"]}

    @app.delete("/api/v1/alert-notify/config", dependencies=[Depends(admin)])
    def clear_config():
        with transaction() as db:
            row = db.execute("SELECT * FROM alert_notify_config WHERE id=1").fetchone()
            if row is None:
                raise HTTPException(404, "No alert-notify configuration")
            db.execute("UPDATE alert_notify_config SET enabled=0,updated_at=? WHERE id=1", (time.time(),))
            audit(db, "alert_notify.cleared", "alert-notify", "admin",
                  {"url": redact_url(row["url"])})
        return {"enabled": False}

    @app.post("/api/v1/alert-notify/test", dependencies=[Depends(admin)])
    def test_push():
        with transaction() as db:
            row = db.execute("SELECT * FROM alert_notify_config WHERE id=1").fetchone()
        if row is None:
            raise HTTPException(409, "No alert-notify configuration")
        try:
            post_once(row["url"], row["channel"], "[测试] AI Ops 告警外推",
                      "这是一条测试推送。若你看到它，说明 Webhook 配置正确。")
        except ValueError as e:
            raise HTTPException(422, str(e))
        except Exception as e:
            raise HTTPException(502, "Delivery failed: " + type(e).__name__)
        return {"sent": True, "channel": row["channel"]}

    @app.get("/api/v1/alert-notify/deliveries", dependencies=[Depends(admin)])
    def deliveries(after: int = 0, limit: int = 100):
        if not 1 <= limit <= 500 or after < 0:
            raise HTTPException(422, "Invalid pagination")
        with transaction() as db:
            rows = db.execute("SELECT * FROM alert_notifications WHERE seq>? ORDER BY seq LIMIT ?",
                              (after, limit)).fetchall()
            return [{**dict(r), "url": redact_url(r["url"])} for r in rows]


def enqueue_notification(db, *, turn_id, dedupe_key, url, channel, title, body, alarm_state=None):
    """Insert one durable push row. Returns the row id, or None if deduped.

    Deduplication is deliberately minimal (docs/alert-webhook.md): Alertmanager
    already owns real de-noising; we only refuse to push the same key twice, so a
    duplicate delivery does not become a duplicate conclusion for the user.
    """
    existing = db.execute("SELECT id FROM alert_notifications WHERE dedupe_key=?", (dedupe_key,)).fetchone()
    if existing is not None:
        return None
    notify_id = str(uuid.uuid4())
    now = time.time()
    db.execute("INSERT INTO alert_notifications(id,turn_id,dedupe_key,url,channel,title,body,alarm_state,status,next_attempt_at,created_at) "
               "VALUES(?,?,?,?,?,?,?,?,'pending',?,?)",
               (notify_id, turn_id, dedupe_key, url, channel, title, body[:MAX_BODY], alarm_state, now, now))
    return notify_id


def _first_alarm_state(payload):
    """Best-effort firing/resolved detection from an Alertmanager-shaped payload."""
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if isinstance(status, str) and status.lower() in ("firing", "resolved"):
        return status.lower()
    common = payload.get("commonAnnotations") or payload.get("commonLabels")
    if isinstance(common, dict):
        s = common.get("status")
        if isinstance(s, str) and s.lower() in ("firing", "resolved"):
            return s.lower()
    return None


def _conclusion_body(role_id, state, final_text, source, alarm_state):
    """Compose the push body. This is a CONCLUSION, not the raw alarm."""
    lines = []
    if alarm_state == "resolved":
        lines.append("当前状态：已恢复（Alertmanager 报告 resolved；若为分析期间恢复，判为疑似瞬时抖动）。")
    elif alarm_state == "firing":
        lines.append("当前状态：仍在告警中（Alertmanager 报告 firing）。")
    lines.append("来源告警：%s" % (source or "unknown"))
    lines.append("处理角色：%s" % role_id)
    if state == "completed":
        lines.append("")
        lines.append(final_text or "（模型未给出文字结论。）")
    elif state == "blocked_unknown":
        lines.append("AI 排查未完成：执行结果不确定，需人工在“需人工处置”中确认。")
    elif state == "cancelled":
        lines.append("AI 排查被取消，未产生结论。")
    else:
        lines.append("AI 排查失败，未产生结论（请以 Alertmanager 原始告警为准）。")
    return "\n".join(lines)


def collect_conclusions(transaction, audit, now=None):
    """Turn finished alarm turns into durable push rows. Returns rows created.

    Runs inside the same transaction as the read so the 'no row yet' check cannot
    race a second worker into creating two rows for one turn (dedupe_key=turn_id).
    """
    now = time.time() if now is None else now
    created = 0
    with transaction() as db:
        cfg = db.execute("SELECT * FROM alert_notify_config WHERE id=1 AND enabled=1").fetchone()
        if cfg is None:
            return 0
        for row in db.execute(COLLECT_SQL).fetchall():
            payload_row = db.execute("SELECT payload FROM alarm_log WHERE turn_id=? LIMIT 1",
                                     (row["turn_id"],)).fetchone()
            try:
                payload = json.loads(payload_row["payload"]) if payload_row else {}
            except (ValueError, TypeError):
                payload = {}
            alarm_state = _first_alarm_state(payload)
            title = "[已分析] " + (row["title"] or "告警")
            body = _conclusion_body(row["role_id"], row["turn_state"], row["final_text"],
                                    row["source"], alarm_state)
            notify_id = enqueue_notification(
                db, turn_id=row["turn_id"], dedupe_key="turn:" + row["turn_id"],
                url=cfg["url"], channel=cfg["channel"], title=title, body=body,
                alarm_state=alarm_state)
            if notify_id is not None:
                audit(db, "alert_notify.queued", notify_id, "service",
                      {"turn_id": row["turn_id"], "channel": cfg["channel"], "turn_state": row["turn_state"]})
                created += 1
    return created


def deliver_pending(transaction, audit, now=None):
    """Push pending rows; bounded retries; never leak the URL secret on failure."""
    now = time.time() if now is None else now
    with transaction() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM alert_notifications WHERE status='pending' AND next_attempt_at<=? "
            "ORDER BY seq LIMIT 50", (now,))]
    delivered = 0
    for row in rows:
        try:
            post_once(row["url"], row["channel"], row["title"] or "AI Ops 告警结论", row["body"])
            error = None
        except ValueError as e:
            error = str(e)          # SSRF_* — a permanent, non-retryable refusal
        except Exception as e:
            error = type(e).__name__
        with transaction() as db:
            current = db.execute("SELECT * FROM alert_notifications WHERE id=?", (row["id"],)).fetchone()
            if current is None or current["status"] != "pending":
                continue
            if error is None:
                db.execute("UPDATE alert_notifications SET status='delivered',delivered_at=?,attempts=attempts+1,last_error=NULL WHERE id=?",
                           (time.time(), row["id"]))
                audit(db, "alert_notify.delivered", row["id"], "service",
                      {"turn_id": row["turn_id"], "channel": row["channel"]})
                delivered += 1
            else:
                attempts = current["attempts"] + 1
                permanent = error.startswith("SSRF_")
                if permanent or attempts >= MAX_ATTEMPTS:
                    db.execute("UPDATE alert_notifications SET status='failed',attempts=?,last_error=? WHERE id=?",
                               (attempts, error, row["id"]))
                    audit(db, "alert_notify.failed", row["id"], "service",
                          {"turn_id": row["turn_id"], "reason": error, "attempts": attempts})
                else:
                    backoff = min(300, 2 ** min(attempts, 8))
                    db.execute("UPDATE alert_notifications SET attempts=?,last_error=?,next_attempt_at=? WHERE id=?",
                               (attempts, error, now + backoff, row["id"]))
                    audit(db, "alert_notify.retry", row["id"], "service",
                          {"turn_id": row["turn_id"], "reason": error, "attempt": attempts})
    return delivered


def start_alert_notify_worker(transaction, audit):
    """Background thread: deliver pending pushes. Never turns a prompt into a command."""
    import logging
    import threading

    log = logging.getLogger(__name__)
    stop = threading.Event()

    def run():
        while not stop.is_set():
            try:
                collect_conclusions(transaction, audit)
                deliver_pending(transaction, audit)
            except Exception as e:
                log.warning("Alert notify iteration failed: %s", type(e).__name__)
            stop.wait(2)

    thread = threading.Thread(target=run, name="alert-notify-worker", daemon=True)
    thread.start()
    return stop, thread
