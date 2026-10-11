"""Release regressions: validate the installed UI entry point, not just HTTP 200."""
import hashlib
import re
from pathlib import Path

from fastapi.testclient import TestClient

from ai_ops.app import create_app
from ai_ops.console import ASSETS, console_dir

ADMIN = "console-release-test-only-1234567890"


def test_ui_release_matches_installed_files_and_revalidates(tmp_path):
    client = TestClient(create_app(str(tmp_path / "control.db"), ADMIN))
    page = client.get("/")
    assert page.headers["cache-control"] == "no-store"
    revision = page.headers["x-ai-ops-ui-revision"]
    expected = hashlib.sha256(b"".join(
        name.encode() + b"\0" + (console_dir() / name).read_bytes()
        for name in sorted(ASSETS)
    )).hexdigest()[:16]
    assert revision == expected
    assert "__AI_OPS_UI_REVISION__" not in page.text
    assert "?v=6" not in page.text
    assert revision in page.text
    urls = re.findall(r'(?:href|src)="(/assets/[^\"]+)"', page.text)
    assert set(urls) == {f"/assets/{revision}/app.js", f"/assets/{revision}/app.css"}
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert response.headers["x-ai-ops-ui-revision"] == revision
        assert response.content == (console_dir() / url.rsplit("/", 1)[-1]).read_bytes()
    for name in ("app.js", "app.css"):
        assert client.get("/assets/" + name).headers["cache-control"] == "no-store"
    assert client.get("/assets/obsolete/app.js").status_code == 404
    assert client.get(f"/assets/{revision}/console.py").status_code == 404
    assert client.get("/api/v1/roles").status_code in (401, 403)
    assert ADMIN not in page.text


def test_installed_script_uses_flat_navigation_and_rework_contract():
    script = (console_dir() / "app.js").read_text(encoding="utf-8")
    views = re.search(r"const VIEWS = (.*);", script).group(1)
    assert '"memory"' not in views
    for item in ("资产", "聊天", "定制任务", "Skills", "文档", "日志"):
        assert item in views
    for old in ("总览", "需人工处置", "控制台", '"tasks"', '"roles"', '"channels"'):
        assert old not in views
    for contract in ("heartbeat_attempt_receipt", "execution_state", "taskActionCard", "system-asset-inventory", "role-avatar", "高级：接收与 ACK 证据", "notification-options", "stepExpansion"):
        assert contract in script
