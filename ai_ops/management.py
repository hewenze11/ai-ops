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
import json
import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .models import Identifier as ID, UserName as USER

MAX_SKILL_CHARS = 100000

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


class AssetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=200)
    allowed_users: list[USER] | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=16000)


def install_management(app, transaction, audit, admin):
    def require_role(db, role_id):
        if not db.execute("SELECT 1 FROM roles WHERE id=?", (role_id,)).fetchone():
            raise HTTPException(404, "Role not found")

    # ---- roles -------------------------------------------------------------
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

    return {"view_skill": lambda row: dict(row)}
