"""Admin management surface for the Web console: roles, asset notes, documents
and local Skills.

The console was read-first on purpose. This module adds the WRITE operations the
operator actually needs from a browser, without inventing new authority: every
route below requires the same administrative credential as the equivalent
existing endpoint. Nothing here can execute a command on a host.

Skills are a local, injected instruction bundle. They are data, not code: a skill
is a named block of text plus the roles it applies to. The turn context injects
the skill text for the current role the same way it injects documents, and a
skill can NEVER widen authority (it cannot add accounts, change the mode, or grant
tools). Treat skill text as untrusted reference material, not as permissions.
"""
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.request

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .custom_tasks import CustomTask, trigger_path
from .models import Identifier as ID, UserName as USER

MAX_SKILL_CHARS = 100000

# A subscriber pulls Skills from the website's hub. The hub is the authority on
# WHICH skills a subscriber may see (it checks the pull key and the subscription
# upstream); the control service only decides WHICH local roles to bind them to.
# A pulled skill can therefore never widen authority here: even a malicious hub
# response only becomes inert reference text for roles the operator already has.
SKILL_SYNC_TIMEOUT_SECONDS = 20
MAX_SKILL_SYNC_BYTES = 4 * 1024 * 1024
DEFAULT_SKILL_HUB_URL = "https://example.invalid/api/v1/skills/repo"

SCHEMA = """
CREATE TABLE IF NOT EXISTS skills(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, content TEXT NOT NULL,
 role_ids TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 revision INTEGER NOT NULL DEFAULT 1, deleted_at REAL);
"""


class Skill(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: ID
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=MAX_SKILL_CHARS)
    role_ids: list[ID] = Field(default_factory=list, max_length=100)
    enabled: bool = True


class RoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)


class RoleCreate(BaseModel):
    """Create a role from the console.

    No credential is minted here: a role only holds a name until an operator
    configures its model and memory separately, so there is nothing secret to
    hand back. This is a convenience wrapper over the same admin POST route.
    """
    model_config = ConfigDict(extra="forbid")
    id: ID
    name: str = Field(min_length=1, max_length=200)


class AssetCreate(BaseModel):
    """Register an asset from the console. The agent token is returned once.

    SSH fields mirror the core Asset model. The secret itself never travels
    through this route: ``ssh_secret_ref`` is a pointer to a secret file the
    operator placed on the server, exactly as the admin API expects.
    """
    model_config = ConfigDict(extra="forbid")
    id: ID
    name: str = Field(min_length=1, max_length=200)
    connection_type: str = Field(default="agent", pattern="^(agent|ssh)$")
    allowed_users: list[USER] = Field(min_length=1, max_length=100)
    notes: str = Field(default="", max_length=16000)
    ssh_host: str | None = Field(default=None, max_length=255)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str | None = Field(default=None, max_length=64)
    ssh_auth_kind: str | None = Field(default=None, pattern="^(key|password)$")
    ssh_secret_ref: str | None = Field(default=None, max_length=512)
    ssh_host_key: str | None = Field(default=None, max_length=2000)


class AssetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=200)
    allowed_users: list[USER] | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=16000)


class SkillSyncIn(BaseModel):
    """Pull Skills from the website's hub and import them locally.

    ``source_url`` is the hub endpoint (defaults to the documented path). The
    pull key travels in the Authorization header, never in the URL or audit log.
    ``role_ids`` optionally overrides every pulled skill's role binding; when
    omitted, each skill keeps the roles the hub sent, minus any role this
    instance does not define (unknown roles are reported, not auto-created).
    """
    model_config = ConfigDict(extra="forbid")
    source_url: str = Field(default="", max_length=500)
    pull_key: str = Field(min_length=8, max_length=300)
    role_ids: list[ID] | None = Field(default=None, max_length=100)
    dry_run: bool = False


# The fields the control service stores for a skill. Anything else the hub
# sends (a `revision`, a `tier`, a future field) is dropped at the boundary.
_SKILL_FIELDS = ("id", "name", "content", "role_ids", "enabled")


def _project_skill(raw) -> dict:
    """Keep only the fields this service understands; leave the rest behind."""
    if not isinstance(raw, dict):
        return {"_invalid": True}
    projected = {k: raw[k] for k in _SKILL_FIELDS if k in raw}
    # `enabled` is hub-optional; default to on when absent.
    projected.setdefault("enabled", True)
    return projected


def fetch_skill_bundle(source_url: str, pull_key: str, *, timeout: int = SKILL_SYNC_TIMEOUT_SECONDS,
                       opener=None) -> dict:
    """Fetch the hub's skill bundle. Isolated so tests can inject a fake opener.

    Raises HTTPException with a precise status so the operator can tell apart
    "your subscription lapsed" (402) from "your key is wrong" (403). The response
    body is size-capped: a hostile or broken hub cannot exhaust our memory.
    """
    if not source_url:
        raise HTTPException(422, "source_url is required (the website's /api/v1/skills/repo)")
    req = urllib.request.Request(source_url, headers={
        "Authorization": "Bearer " + pull_key,
        "Accept": "application/json",
        "User-Agent": "ai-ops-control/skill-sync",
    })
    try:
        if opener is None:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(MAX_SKILL_SYNC_BYTES + 1)
        else:
            with opener(req, timeout=timeout) as resp:
                raw = resp.read(MAX_SKILL_SYNC_BYTES + 1)
    except urllib.error.HTTPError as e:
        # 401 missing key, 402 subscription expired, 403 bad key — surface as-is.
        detail = "skill hub rejected the pull key"
        if e.code == 402:
            detail = "Skills subscription is expired or inactive"
        elif e.code == 403:
            detail = "pull key is invalid or revoked"
        raise HTTPException(e.code if 400 <= e.code < 500 else 502, detail)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HTTPException(502, f"could not reach the skill hub: {e}")
    if len(raw) > MAX_SKILL_SYNC_BYTES:
        raise HTTPException(502, "skill hub response too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(502, "skill hub returned invalid JSON")
    skills = payload.get("skills")
    if not isinstance(skills, list):
        raise HTTPException(502, "skill hub response missing a skills list")
    return payload


def install_management(app, transaction, audit, admin):
    def require_role(db, role_id):
        if not db.execute("SELECT 1 FROM roles WHERE id=?", (role_id,)).fetchone():
            raise HTTPException(404, "Role not found")

    # ---- roles -------------------------------------------------------------
    @app.post("/api/v1/console/roles", dependencies=[Depends(admin)], status_code=201)
    def create_role(body: RoleCreate):
        with transaction() as db:
            if db.execute("SELECT 1 FROM roles WHERE id=?", (body.id,)).fetchone():
                raise HTTPException(409, "Role already exists")
            db.execute("INSERT INTO roles(id,name) VALUES(?,?)", (body.id, body.name))
            audit(db, "role.created", body.id, "admin", body.model_dump())
        return {"id": body.id, "name": body.name}

    @app.put("/api/v1/roles/{role_id}", dependencies=[Depends(admin)])
    def rename_role(role_id: ID, body: RoleUpdate):
        with transaction() as db:
            require_role(db, role_id)
            db.execute("UPDATE roles SET name=? WHERE id=?", (body.name, role_id))
            audit(db, "role.renamed", role_id, "admin", body.model_dump())
        return {"id": role_id, "name": body.name}

    # ---- assets ------------------------------------------------------------
    @app.put("/api/v1/assets/{asset_id}/notes", dependencies=[Depends(admin)])
    def update_asset(asset_id: ID, body: AssetUpdate):
        with transaction() as db:
            row = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Asset not found")
            name = body.name if body.name is not None else row["name"]
            users = body.allowed_users if body.allowed_users is not None else json.loads(row["allowed_users"])
            notes = body.notes if body.notes is not None else row["notes"]
            if len(set(users)) != len(users):
                raise HTTPException(422, "Duplicate allowed users")
            db.execute("UPDATE assets SET name=?,allowed_users=?,notes=? WHERE id=?",
                       (name, json.dumps(users), notes, asset_id))
            audit(db, "asset.updated", asset_id, "admin",
                  {"name": name, "allowed_users": users, "notes": notes})
        return {"id": asset_id, "name": name, "allowed_users": users, "notes": notes}

    # This route existed on the core app already; the console still calls the
    # original POST /api/v1/assets. We re-register a console-facing alias so the
    # one-time agent token is returned through a path the console owns, keeping
    # the SSH key material out of the browser entirely.
    @app.post("/api/v1/console/assets", dependencies=[Depends(admin)], status_code=201)
    def create_asset(body: AssetCreate):
        from .app import Asset

        token = secrets.token_urlsafe(36)
        with transaction() as db:
            if db.execute("SELECT 1 FROM assets WHERE id=?", (body.id,)).fetchone():
                raise HTTPException(409, "Asset already exists")
            if len(set(body.allowed_users)) != len(body.allowed_users):
                raise HTTPException(422, "Duplicate allowed users")
            try:
                asset = Asset(id=body.id, name=body.name, connection_type=body.connection_type,
                              allowed_users=body.allowed_users, notes=body.notes,
                              ssh_host=body.ssh_host, ssh_port=body.ssh_port, ssh_user=body.ssh_user,
                              ssh_auth_kind=body.ssh_auth_kind, ssh_secret_ref=body.ssh_secret_ref,
                              ssh_host_key=body.ssh_host_key)
            except ValueError as e:
                raise HTTPException(422, str(e))
            db.execute("INSERT INTO assets(id,name,allowed_users,notes,token_hash,connection_type) VALUES(?,?,?,?,?,?)",
                       (body.id, body.name, json.dumps(body.allowed_users), body.notes,
                        hashlib.sha256(token.encode()).hexdigest(), body.connection_type))
            if body.connection_type == "ssh":
                from .connector_ssh import save_connection
                save_connection(db, asset)
            audit(db, "asset.provisioned", body.id, "admin",
                  {"name": body.name, "connection_type": body.connection_type, "via": "console"})
        response = {"asset_id": body.id, "connection_type": body.connection_type}
        if body.connection_type == "agent":
            response["agent_token"] = token
        return response

    # ---- custom tasks ------------------------------------------------------
    # The console can create and edit custom tasks. The trigger token that the
    # core POST returns is a credential, so the console mirrors the same rule:
    # create returns it ONCE, edit never re-returns it (a fresh one only via the
    # dedicated rotate route, if an operator ever needs it).
    @app.post("/api/v1/console/custom-tasks", dependencies=[Depends(admin)], status_code=201)
    def create_task(body: CustomTask):
        from .custom_tasks import next_fire

        secret = secrets.token_urlsafe(36)
        now = time.time()
        due = next_fire(body.cron, body.timezone, now) if body.kind == "scheduled" and body.enabled else None
        with transaction() as db:
            require_role(db, body.role_id)
            if db.execute("SELECT 1 FROM custom_tasks WHERE id=?", (body.id,)).fetchone():
                raise HTTPException(409, "Custom task ID already exists, including deleted records")
            db.execute("INSERT INTO custom_tasks VALUES(?,?,?,?,?,?,?,?)",
                       (body.id, body.model_dump_json(), 1, hashlib.sha256(secret.encode()).hexdigest(), due, None, now, now))
            audit(db, "custom_task.created", body.id, "admin", {**body.model_dump(), "via": "console"})
        return {"id": body.id, "trigger_token": secret, "trigger_path": trigger_path(body.id),
                "next_fire_at": due}

    @app.put("/api/v1/console/custom-tasks/{custom_id}", dependencies=[Depends(admin)])
    def edit_task(custom_id: ID, body: CustomTask):
        from .custom_tasks import next_fire

        if custom_id != body.id:
            raise HTTPException(422, "ID must match path")
        now = time.time()
        with transaction() as db:
            require_role(db, body.role_id)
            old = db.execute("SELECT * FROM custom_tasks WHERE id=? AND deleted_at IS NULL", (custom_id,)).fetchone()
            if old is None:
                raise HTTPException(404, "Custom task not found")
            before = json.loads(old["config"])
            schedule_keys = ("kind", "cron", "timezone", "enabled")
            if all(before[k] == body.model_dump()[k] for k in schedule_keys):
                due = old["next_fire_at"]
            else:
                due = next_fire(body.cron, body.timezone, now) if body.kind == "scheduled" and body.enabled else None
            db.execute("UPDATE custom_tasks SET config=?,revision=revision+1,next_fire_at=?,updated_at=? WHERE id=?",
                       (body.model_dump_json(), due, now, custom_id))
            if not body.enabled:
                db.execute("UPDATE schedule_outbox SET state='cancelled',last_error='CUSTOM_TASK_DISABLED' WHERE custom_task_id=? AND state='pending'", (custom_id,))
            audit(db, "custom_task.updated", custom_id, "admin", {"before": before, "after": body.model_dump()})
        # The trigger token is deliberately NOT returned on edit.
        return {"id": custom_id, "next_fire_at": due, "trigger_path": trigger_path(custom_id),
                "token_unchanged": True}
    # ---- skills ------------------------------------------------------------
    @app.get("/api/v1/skills", dependencies=[Depends(admin)])
    def list_skills():
        with transaction() as db:
            return [{**dict(r), "role_ids": json.loads(r["role_ids"]), "enabled": bool(r["enabled"])}
                    for r in db.execute("SELECT * FROM skills WHERE deleted_at IS NULL ORDER BY id")]

    @app.put("/api/v1/skills/{skill_id}", dependencies=[Depends(admin)])
    def save_skill(skill_id: ID, body: Skill):
        if skill_id != body.id:
            raise HTTPException(422, "Skill ID mismatch")
        with transaction() as db:
            for role in body.role_ids:
                require_role(db, role)
            db.execute(
                "INSERT INTO skills(id,name,content,role_ids,enabled,revision,deleted_at) VALUES(?,?,?,?,?,1,NULL) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,content=excluded.content,"
                "role_ids=excluded.role_ids,enabled=excluded.enabled,revision=skills.revision+1,deleted_at=NULL",
                (body.id, body.name, body.content, json.dumps(body.role_ids), int(body.enabled)))
            audit(db, "skill.saved", body.id, "admin", {"name": body.name, "role_ids": body.role_ids})
            row = db.execute("SELECT * FROM skills WHERE id=?", (body.id,)).fetchone()
            return {**dict(row), "role_ids": json.loads(row["role_ids"]), "enabled": bool(row["enabled"])}

    @app.delete("/api/v1/skills/{skill_id}", dependencies=[Depends(admin)])
    def delete_skill(skill_id: ID):
        with transaction() as db:
            if not db.execute("UPDATE skills SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
                              (time.time(), skill_id)).rowcount:
                raise HTTPException(404, "Skill not found")
            audit(db, "skill.deleted", skill_id, "admin", {})
        return {"deleted": True, "audit_retained": True}

    # ---- skills: pull from the website hub ----------------------
    @app.post("/api/v1/skills/sync", dependencies=[Depends(admin)])
    def sync_skills(body: SkillSyncIn):
        """Import the subscriber's Skills from the website hub.

        The pull key is used only to authenticate the outbound request; it is
        never logged or echoed. A pulled skill may only be bound to roles this
        instance already defines; any role the hub sent that we do not have is
        reported and dropped (we never auto-create roles from remote input).
        """
        bundle = fetch_skill_bundle(body.source_url, body.pull_key)
        imported: list[dict] = []
        skipped: list[dict] = []
        with transaction() as db:
            known_roles = {r["id"] for r in db.execute("SELECT id FROM roles")}
            if body.role_ids is not None:
                for role in body.role_ids:
                    require_role(db, role)
            for raw in bundle["skills"]:
                try:
                    # The hub may carry fields that only mean something upstream
                    # (e.g. `revision`). Project onto the fields the control
                    # service actually stores, so a hub-side addition never
                    # breaks the sync. We keep Skill's extra="forbid" so a
                    # genuinely malformed payload is still rejected loudly.
                    skill = Skill.model_validate(_project_skill(raw))
                except Exception as e:
                    skipped.append({"id": raw.get("id") if isinstance(raw, dict) else None,
                                    "reason": f"invalid skill payload: {e}"})
                    continue
                requested = body.role_ids if body.role_ids is not None else skill.role_ids
                bound = [r for r in requested if r in known_roles]
                dropped = [r for r in requested if r not in known_roles]
                if not bound:
                    skipped.append({"id": skill.id,
                                    "reason": "no known role to bind (unknown: " +
                                              ",".join(dropped) + ")" if dropped
                                              else "no role bound and none given"})
                    continue
                if body.dry_run:
                    imported.append({"id": skill.id, "name": skill.name, "role_ids": bound,
                                     "would_apply": True})
                    continue
                db.execute(
                    "INSERT INTO skills(id,name,content,role_ids,enabled,revision,deleted_at) "
                    "VALUES(?,?,?,?,?,1,NULL) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name,content=excluded.content,"
                    "role_ids=excluded.role_ids,enabled=excluded.enabled,"
                    "revision=skills.revision+1,deleted_at=NULL",
                    (skill.id, skill.name, skill.content, json.dumps(bound), int(skill.enabled)))
                imported.append({"id": skill.id, "name": skill.name, "role_ids": bound,
                                 "dropped_roles": dropped})
            if not body.dry_run:
                audit(db, "skill.synced", body.source_url or DEFAULT_SKILL_HUB_URL, "admin",
                      {"imported": [s["id"] for s in imported],
                       "skipped": [s.get("id") for s in skipped],
                       "roles_override": body.role_ids})
        return {"dry_run": body.dry_run, "bundle_revision": bundle.get("bundle_revision"),
                "imported": imported, "skipped": skipped,
                "counts": {"imported": len(imported), "skipped": len(skipped)}}

    return {"view_skill": lambda row: dict(row)}
