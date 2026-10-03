"""Messaging channel gateway: Feishu / WeCom / Weixin share the SAME role memory.

Design in one paragraph
-----------------------
A channel identity (``channel`` + ``user_id``) is mapped to an AI Ops role. A
message arriving from that identity is enqueued as an ordinary role turn with
``caller = "channel:<name>"``. Because the role turn queue and the daily memory
are keyed by ``role_id`` — never by channel — the same role sees the same memory
and the same serial queue no matter whether the message came from Web, Feishu,
WeCom or Weixin. That IS the "channels share role memory" requirement, and it
falls out of the existing model rather than being bolted on.

Boundaries that are deliberate
------------------------------
* **The one-time pairing code is the authentication.** A brand-new channel
  identity cannot act until an operator issues a single-use code for a role and
  the caller presents it. We never auto-create a role from an unknown sender:
  an unauthenticated inbound message is recorded and ignored.
* **Weixin has no public outbound API in this build.** Messages are stored as
  outbound deliveries and can be retrieved per identity; actually pushing them
  to Weixin requires a transport this repository does not ship. We never claim a
  message was sent when we only stored it.
* **Payloads and command output are untrusted data.** A channel message is
  just a prompt; it cannot change mode, accounts or role via message content.
* **Role reply text can contain command output.** Delivery therefore carries the
  role's final summary. Outbound text is returned to the operator-side channel
  bridge over the authenticated channel API; a role's private conversation is
  never exposed to another channel.
"""
import hashlib
import hmac
import json
import secrets
import time

from fastapi import Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .models import Identifier as ID

# Channel names are a small closed set for now; a new transport is a real plumbing
# change, so we validate rather than accept arbitrary strings.
CHANNELS = ("feishu", "wecom", "weixin")
MAX_TEXT = 8000

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_identities(
 channel TEXT NOT NULL, user_id TEXT NOT NULL, role_id TEXT NOT NULL REFERENCES roles(id),
 mode TEXT NOT NULL, execution_users TEXT NOT NULL,
 created_at REAL NOT NULL, last_seen REAL,
 PRIMARY KEY(channel, user_id));
CREATE TABLE IF NOT EXISTS channel_pairing(
 code_hash TEXT PRIMARY KEY, role_id TEXT NOT NULL, mode TEXT NOT NULL,
 execution_users TEXT NOT NULL, expires_at REAL NOT NULL, used_at REAL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS channel_messages(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, channel TEXT NOT NULL,
 user_id TEXT NOT NULL, direction TEXT NOT NULL, role_id TEXT, turn_id TEXT, text TEXT NOT NULL,
 created_at REAL NOT NULL, delivered_at REAL);
CREATE INDEX IF NOT EXISTS channel_inbox ON channel_messages(channel,user_id,direction,seq);
"""


class Pairing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_id: ID
    mode: str = Field(default="confirm")
    execution_users: list[str] = Field(default_factory=list, max_length=100)
    ttl_seconds: int = Field(default=3600, ge=60, le=604800)


class Inbound(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: str = Field(max_length=32)
    user_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    pairing_code: str | None = Field(default=None, max_length=200)
    external_id: str | None = Field(default=None, max_length=200)
    display_name: str | None = Field(default=None, max_length=200)


def install_channels(app, transaction, audit, admin, admin_token_provider):
    """Register channel APIs. ``admin_token_provider`` mirrors custom_tasks: the
    channel bridge authenticates with the CURRENT admin credential, re-read on
    every call so a rotated token is honoured without a restart."""
    if isinstance(admin_token_provider, str):
        admin_token_provider = (lambda value: (lambda: value))(admin_token_provider)

    def bridge(authorization: str | None):
        # The channel bridge is a trusted operator-side component. It presents the
        # administrative credential; end users never see or handle it.
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer authentication required")
        supplied = authorization[7:]
        if not hmac.compare_digest(supplied.encode(), admin_token_provider().encode()):
            raise HTTPException(403, "Invalid channel bridge credential")

    def view_identity(row):
        return {**dict(row), "execution_users": json.loads(row["execution_users"])}

    @app.post("/api/v1/channels/pairings", dependencies=[Depends(admin)], status_code=201)
    def create_pairing(body: Pairing):
        if body.mode not in ("readonly", "confirm", "direct"):
            raise HTTPException(422, "Unknown mode")
        code = secrets.token_urlsafe(18)
        now = time.time()
        with transaction() as db:
            if not db.execute("SELECT 1 FROM roles WHERE id=?", (body.role_id,)).fetchone():
                raise HTTPException(404, "Role not found")
            db.execute("INSERT INTO channel_pairing VALUES(?,?,?,?,?,NULL,?)",
                       (hashlib.sha256(code.encode()).hexdigest(), body.role_id, body.mode,
                        json.dumps(body.execution_users), now + body.ttl_seconds, now))
            audit(db, "channel.pairing_created", body.role_id, "admin",
                  {"mode": body.mode, "ttl_seconds": body.ttl_seconds})
        # The code is shown once, like a trigger token; only its hash is stored.
        return {"pairing_code": code, "role_id": body.role_id, "mode": body.mode,
                "expires_at": now + body.ttl_seconds}

    @app.get("/api/v1/channels/identities", dependencies=[Depends(admin)])
    def list_identities(channel: str | None = None):
        with transaction() as db:
            rows = db.execute("SELECT * FROM channel_identities WHERE (? IS NULL OR channel=?) ORDER BY last_seen DESC",
                              (channel, channel)).fetchall()
            return [view_identity(r) for r in rows]

    @app.delete("/api/v1/channels/identities/{channel}/{user_id}", dependencies=[Depends(admin)])
    def unbind(channel: str, user_id: str):
        with transaction() as db:
            if not db.execute("DELETE FROM channel_identities WHERE channel=? AND user_id=?", (channel, user_id)).rowcount:
                raise HTTPException(404, "Channel identity not found")
            audit(db, "channel.unbound", user_id, "admin", {"channel": channel})
        return {"unbound": True, "audit_retained": True}

    @app.post("/api/v1/channels/inbound")
    async def inbound(request: Request, authorization: str | None = Header(default=None)):
        bridge(authorization)
        try:
            payload = json.loads(await request.body() or b"{}")
        except ValueError:
            raise HTTPException(422, "Body must be JSON")
        try:
            body = Inbound.model_validate(payload)
        except Exception as error:
            raise HTTPException(422, str(error))
        if body.channel not in CHANNELS:
            raise HTTPException(422, "Unsupported channel")

        now = time.time()
        with transaction() as db:
            identity = db.execute("SELECT * FROM channel_identities WHERE channel=? AND user_id=?",
                                  (body.channel, body.user_id)).fetchone()
            # Pairing: if a code is presented, bind this identity to its role.
            if body.pairing_code:
                row = db.execute("SELECT * FROM channel_pairing WHERE code_hash=?",
                                 (hashlib.sha256(body.pairing_code.encode()).hexdigest(),)).fetchone()
                if row is None:
                    raise HTTPException(403, "Unknown pairing code")
                if row["used_at"] is not None:
                    raise HTTPException(409, "Pairing code already used")
                if row["expires_at"] < now:
                    raise HTTPException(410, "Pairing code expired")
                db.execute("INSERT INTO channel_identities VALUES(?,?,?,?,?,?,?) "
                           "ON CONFLICT(channel,user_id) DO UPDATE SET role_id=excluded.role_id,"
                           "mode=excluded.mode,execution_users=excluded.execution_users,last_seen=excluded.last_seen",
                           (body.channel, body.user_id, row["role_id"], row["mode"], row["execution_users"], now, now))
                db.execute("UPDATE channel_pairing SET used_at=? WHERE code_hash=?", (now, row["code_hash"]))
                audit(db, "channel.paired", body.user_id, "channel-bridge",
                      {"channel": body.channel, "role_id": row["role_id"]})
                identity = db.execute("SELECT * FROM channel_identities WHERE channel=? AND user_id=?",
                                      (body.channel, body.user_id)).fetchone()

            message_id = "msg_" + secrets.token_urlsafe(12)
            if identity is None:
                # Record the unauthenticated inbound message and DO NOT act: an
                # unknown sender must not be able to queue role work or pick a role.
                db.execute("INSERT INTO channel_messages(id,channel,user_id,direction,text,created_at) VALUES(?,?,?,?,?,?)",
                           (message_id, body.channel, body.user_id, "inbound", body.text, now))
                audit(db, "channel.unpaired_message", body.user_id, "channel-bridge", {"channel": body.channel})
                return {"status": "unpaired", "message_id": message_id,
                        "detail": "Identity is not paired with a role; a pairing code is required."}

            # Idempotency: the same upstream event must not create two turns.
            inbound_id = ("ext_" + body.external_id) if body.external_id else message_id
            if body.external_id:
                existing = db.execute("SELECT * FROM channel_messages WHERE id=? AND direction='inbound'",
                                      (inbound_id,)).fetchone()
                if existing:
                    return {"status": "duplicate", "turn_id": existing["turn_id"], "message_id": existing["id"]}

            source_id = "channel:%s:%s" % (body.channel, body.external_id or message_id)
            users = json.loads(identity["execution_users"])
            db.execute("INSERT INTO channel_messages(id,channel,user_id,direction,role_id,text,created_at) VALUES(?,?,?,?,?,?,?)",
                       (inbound_id, body.channel, body.user_id, "inbound", identity["role_id"], body.text, now))
            from .turns import enqueue_turn
            turn_id = enqueue_turn(db, identity["role_id"], "chat", source_id, users,
                                   identity["mode"], body.text, {}, caller="channel:" + body.channel)
            db.execute("UPDATE channel_messages SET turn_id=? WHERE id=?", (turn_id, inbound_id))
            db.execute("UPDATE channel_identities SET last_seen=? WHERE channel=? AND user_id=?",
                       (now, body.channel, body.user_id))
            audit(db, "channel.message_received", turn_id, "channel-bridge",
                  {"channel": body.channel, "user_id": body.user_id, "role_id": identity["role_id"]})
            return {"status": "queued", "turn_id": turn_id, "message_id": inbound_id}

    @app.get("/api/v1/channels/outbox")
    def outbox(channel: str, user_id: str, after: int = 0, limit: int = 50,
               authorization: str | None = Header(default=None), mark: str | None = None):
        """Return role replies for one identity, optionally marking them delivered.

        Turns are the source of truth: a reply exists once its turn completes.
        We never invent a reply for a still-queued or failed turn.
        """
        bridge(authorization)
        if not 1 <= limit <= 500 or after < 0:
            raise HTTPException(422, "Invalid pagination")
        with transaction() as db:
            if not db.execute("SELECT 1 FROM channel_identities WHERE channel=? AND user_id=?",
                              (channel, user_id)).fetchone():
                raise HTTPException(404, "Channel identity not found")
            rows = db.execute(
                "SELECT m.seq,m.id,m.turn_id,m.text,m.delivered_at,t.state,t.final_text,t.error_code "
                "FROM channel_messages m LEFT JOIN role_turns t ON t.id=m.turn_id "
                "WHERE m.channel=? AND m.user_id=? AND m.direction='inbound' "
                "AND m.turn_id IS NOT NULL AND t.state IN ('completed','failed','cancelled','blocked_unknown') "
                "AND m.seq>? ORDER BY m.seq LIMIT ?", (channel, user_id, after, limit)).fetchall()
            result = []
            for row in rows:
                reply = row["final_text"]
                if reply is None:
                    # Honest about non-success: report the state, never a fake reply.
                    reply = "" if row["state"] == "cancelled" else "[%s]" % (row["error_code"] or row["state"])
                result.append({"seq": row["seq"], "turn_id": row["turn_id"], "state": row["state"],
                               "text": reply, "delivered_at": row["delivered_at"]})
            if mark == "1" and result:
                db.execute("UPDATE channel_messages SET delivered_at=? WHERE seq IN (%s)" %
                           ",".join("?" * len(result)), [time.time()] + [r["seq"] for r in result])
        return {"messages": result, "cursor": result[-1]["seq"] if result else after}

    @app.get("/api/v1/channels/conversation", dependencies=[Depends(admin)])
    def conversation(channel: str, user_id: str, limit: int = 100):
        """Admin-side read of one identity's conversation for support/debugging."""
        if not 1 <= limit <= 500:
            raise HTTPException(422, "Invalid limit")
        with transaction() as db:
            rows = db.execute("SELECT * FROM channel_messages WHERE channel=? AND user_id=? ORDER BY seq DESC LIMIT ?",
                              (channel, user_id, limit)).fetchall()
            return [dict(r) for r in rows]

    return {"view_identity": view_identity}
