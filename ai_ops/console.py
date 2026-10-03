"""Read-mostly console surface: static assets plus the few list endpoints the
Web UI needs that were not otherwise exposed.

Two deliberate boundaries:

* Static assets are served WITHOUT the admin credential. The HTML/CSS/JS carry
  no secret, and the page is useless without a token the operator types in. The
  token itself is only ever sent in the ``Authorization`` header by the page.
* The console adds no new authority. Its endpoints either list data that an
  admin endpoint already governs (``/api/v1/roles``, ``/api/v1/console/tasks``)
  or aggregate existing admin reads. Nothing here can execute a command.
"""
import json

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse

ASSETS = {
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}


def console_dir():
    from pathlib import Path
    return Path(__file__).resolve().parent / "console"


def install_console(app, transaction, audit, admin):
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(console_dir() / "index.html", media_type=ASSETS["index.html"])

    @app.get("/assets/app.css", include_in_schema=False)
    def styles():
        return FileResponse(console_dir() / "app.css", media_type=ASSETS["app.css"])

    @app.get("/assets/app.js", include_in_schema=False)
    def script():
        return FileResponse(console_dir() / "app.js", media_type=ASSETS["app.js"])

    @app.get("/api/v1/roles", dependencies=[Depends(admin)])
    def list_roles():
        with transaction() as db:
            return [dict(r) for r in db.execute("SELECT id,name FROM roles ORDER BY id")]

    @app.get("/api/v1/console/tasks", dependencies=[Depends(admin)])
    def list_tasks(limit: int = 100):
        # Newest first for an operator scanning recent activity. Payload is
        # returned as stored (already scrubbed at the boundary for results; the
        # command text itself was never a secret in this preview).
        if not 1 <= limit <= 500:
            raise HTTPException(422, "Invalid limit")
        with transaction() as db:
            return [
                {"id": r["id"], "role_id": r["role_id"], "asset_id": r["asset_id"],
                 "state": r["state"], "payload": json.loads(r["payload"]),
                 "result": json.loads(r["result"]) if r["result"] else None,
                 "turn_id": r["turn_id"], "created_at": r["created_at"], "updated_at": r["updated_at"]}
                for r in db.execute("SELECT * FROM tasks ORDER BY seq DESC LIMIT ?", (limit,))
            ]

    @app.get("/api/v1/console/overview", dependencies=[Depends(admin)])
    def overview():
        with transaction() as db:
            def count(sql, *params):
                return int(db.execute(sql, params).fetchone()[0])
            counts = {
                "roles": count("SELECT COUNT(*) FROM roles"),
                "assets": count("SELECT COUNT(*) FROM assets"),
                "agent_assets": count("SELECT COUNT(*) FROM assets WHERE connection_type='agent'"),
                "ssh_assets": count("SELECT COUNT(*) FROM assets WHERE connection_type='ssh'"),
                "documents": count("SELECT COUNT(*) FROM documents WHERE deleted_at IS NULL"),
                "custom_tasks": count("SELECT COUNT(*) FROM custom_tasks WHERE deleted_at IS NULL"),
                "role_turns": count("SELECT COUNT(*) FROM role_turns"),
                "tasks": count("SELECT COUNT(*) FROM tasks"),
                "alarms": count("SELECT COUNT(*) FROM alarm_log"),
                "audit_events": count("SELECT COUNT(*) FROM audit"),
                "unknown_tasks": count("SELECT COUNT(*) FROM tasks WHERE state='unknown'"),
                "pending_alarms": count("SELECT COUNT(*) FROM alarm_log WHERE state='accepted'"),
            }
        return {
            "counts": counts,
            "notices": [
                "角色注册、资产注册仍是管理员 API（控制台可改名/备注/允许账号与文档/Skill 编辑）",
                "模型梗概当前为确定性占位，非模型生成的语义摘要",
                "渠道：飞书/企业微信/微信共用角色记忆已实现；真实对外发送与平台签名校验属部署侧，本仓库不内置",
            ],
        }

    return {"counts": overview}
