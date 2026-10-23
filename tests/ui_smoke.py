"""Local UI smoke check for the AI-permission and credential-vault pages.

Starts the real app against a throwaway database, seeds one role and one asset,
then drives Chrome through the actual page: open, unlock, click the two new nav
items, and assert the expected controls render. Exits non-zero on any error so
it is usable as a pre-deploy gate.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

TOKEN = "smoke-" + "a" * 40

db = str(Path(tempfile.mkdtemp()) / "smoke.db")
os.environ["AI_OPS_ADMIN_TOKEN"] = TOKEN
os.environ["AI_OPS_DB"] = db

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_ops.app import create_app  # noqa: E402
import uvicorn  # noqa: E402

app = create_app(db, TOKEN, "test-model")
port = 8799
config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
server = uvicorn.Server(config)
threading.Thread(target=server.run, daemon=True).start()

base = f"http://127.0.0.1:{port}"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + TOKEN,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode() or "null")


for _ in range(80):
    try:
        call("GET", "/api/v1/roles")
        break
    except Exception:
        time.sleep(0.15)

call("POST", "/api/v1/roles", {"id": "ops", "name": "运维值班"})
call("POST", "/api/v1/assets", {"id": "web1", "name": "演示 Web", "allowed_users": ["ops_read"]})
print("seeded")

script = Path(__file__).with_name("ui_smoke.cjs")
env = dict(os.environ, SMOKE_TOKEN=TOKEN, SMOKE_BASE=base)
res = subprocess.run(["node", str(script)], env=env, capture_output=True, text=True, timeout=180)
print(res.stdout)
print(res.stderr[-3000:], file=sys.stderr)
server.should_exit = True
sys.exit(res.returncode)
