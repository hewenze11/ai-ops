"""Real end-to-end smoke for alert result fan-out.

Starts the real app, configures a webhook to a LOCAL sink (the shipped SSRF
guard blocks loopback, so we only bypass the guard *in this test process* to
prove the whole pipeline: alarm -> turn completion -> collect -> deliver ->
HTTP POST with the right channel shape). Shipped code is NOT modified.
"""
import http.server
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import ai_ops.alert_notify as an
from ai_ops.app import create_app

ADMIN = "a" * 40
received = []


class Sink(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        received.append(json.loads(body))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):
        pass


def main():
    server = http.server.HTTPServer(("127.0.0.1", 0), Sink)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # Bypass ONLY the SSRF guard for this loopback sink, in-process.
    an._host_is_blocked = lambda host: False

    client = TestClient(create_app(str(Path(tempfile.mkdtemp()) / "control.db"), ADMIN, "test-model"))
    h = {"Authorization": "Bearer " + ADMIN}

    # Configure a generic webhook to the sink.
    r = client.put("/api/v1/alert-notify/config", headers=h,
                   json={"url": f"http://127.0.0.1:{port}/hook", "channel": "generic"})
    assert r.status_code == 200, r.text
    print("PASS config:", r.json())

    client.post("/api/v1/roles", headers=h, json={"id": "ops", "name": "Ops"})
    created = client.post("/api/v1/custom-tasks", headers=h, json={
        "id": "alerts", "name": "Alerts", "kind": "trigger", "role_id": "ops",
        "prompt": "诊断告警", "execution_users": ["ops_read"], "mode": "readonly", "enabled": True})
    token = created.json()["trigger_token"]

    alarm = {"status": "firing", "host": "web-prod-1", "title": "CPU 95%",
             "severity": "critical", "summary": "node cpu above 95% for 5m"}
    r = client.post("/api/v1/triggers/alerts/invoke",
                    headers={"Authorization": "Bearer " + token, "X-Event-ID": "e2e-1"}, json=alarm)
    assert r.status_code == 202, r.text
    turn_id = r.json()["turn_id"]
    print("PASS alarm accepted, turn", turn_id)

    # Simulate the model worker finishing the turn with a conclusion.
    with client.app.state.transaction() as db:
        db.execute("UPDATE role_turns SET state='completed',final_text=?,updated_at=? WHERE id=?",
                   ("根因：发布后某进程 CPU 跑满。建议：先限流，再回滚最近一次发布。", time.time(), turn_id))

    # Drive the worker's two phases directly (same functions the thread calls).
    n = an.collect_conclusions(client.app.state.transaction, client.app.state.audit)
    print("PASS collect ->", n, "push row(s)")
    d = an.deliver_pending(client.app.state.transaction, client.app.state.audit)
    print("PASS deliver ->", d)

    assert received, "sink received nothing"
    payload = received[0]
    print("PASS sink payload keys:", sorted(payload.keys()))
    assert payload["title"].startswith("[已分析]")
    assert "根因" in payload["text"] and "仍在告警中" in payload["text"]

    rows = client.get("/api/v1/alert-notify/deliveries", headers=h).json()
    assert rows[0]["status"] == "delivered", rows
    print("PASS delivery row status:", rows[0]["status"], "| channel:", rows[0]["channel"])

    # --- channel shape checks (per-channel body) ---
    def body_for(channel, url):
        srv = http.server.HTTPServer(("127.0.0.1", 0), Sink)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        an.post_once(f"http://127.0.0.1:{srv.server_address[1]}/x", channel, "T", "B")
        return received[-1]

    fb = body_for("feishu", None)
    assert fb["msg_type"] == "text" and "T" in fb["content"]["text"]
    db_ = body_for("dingtalk", None)
    assert db_["msgtype"] == "markdown" and db_["markdown"]["title"] == "T"
    wc = body_for("wecom", None)
    assert wc["msgtype"] == "markdown" and "T" in wc["markdown"]["content"]
    sl = body_for("slack", None)
    assert "T" in sl["text"]
    dc = body_for("discord", None)
    assert "T" in dc["content"]
    print("PASS channel shapes: feishu/dingtalk/wecom/slack/discord")

    # --- SSRF guard really refuses metadata/loopback with the real function ---
    import importlib
    importlib.reload(an)
    for bad in ("http://169.254.169.254/x", "http://127.0.0.1/x", "file:///etc/passwd"):
        try:
            an.validate_url(bad)
            raise AssertionError("guard failed for " + bad)
        except ValueError as e:
            assert str(e).startswith("SSRF_"), e
    print("PASS SSRF guard refuses metadata/loopback/file in real code")

    server.shutdown()
    print("ALL E2E PASS")


if __name__ == "__main__":
    main()
