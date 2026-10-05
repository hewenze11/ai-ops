"""Real-server smoke test: pushed alert conclusion carries a console deep link."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ADMIN = "smoke-admin-credential-0123456789abcdef"
failures = []


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


def main():
    tmp = tempfile.mkdtemp(prefix="aiops-deeplink-")
    os.environ["AI_OPS_DB"] = os.path.join(tmp, "control.db")
    from fastapi.testclient import TestClient
    from ai_ops.app import create_app
    from ai_ops import alert_notify

    app = create_app(os.path.join(tmp, "control.db"), ADMIN)
    client = TestClient(app)
    h = {"Authorization": "Bearer " + ADMIN}

    # Console URL pointing at a private host (the normal self-hosted case).
    r = client.put("/api/v1/alert-notify/config", headers=h, json={
        "url": "https://open.feishu.cn/open-apis/bot/v2/hook/smoke",
        "channel": "auto", "console_url": "http://10.0.0.5:8765/"})
    check("config accepted with private console_url", r.status_code == 200)
    check("console_url echoed back", r.json().get("console_url") == "http://10.0.0.5:8765/")

    got = client.get("/api/v1/alert-notify/config", headers=h).json()
    check("config roundtrip keeps console_url", got.get("console_url") == "http://10.0.0.5:8765/")

    bad = client.put("/api/v1/alert-notify/config", headers=h, json={
        "url": "https://open.feishu.cn/open-apis/bot/v2/hook/smoke", "channel": "auto",
        "console_url": "mailto:ops@example.com"})
    check("non-http(s) console_url rejected", bad.status_code == 422)

    # Drive a turn to completion and collect the pushed conclusion.
    client.post("/api/v1/roles", headers=h, json={"id": "ops", "name": "Ops"})
    created = client.post("/api/v1/custom-tasks", headers=h, json={
        "id": "alerts", "name": "Alerts", "kind": "trigger", "role_id": "ops",
        "prompt": "诊断告警", "execution_users": ["ops_read"], "mode": "readonly", "enabled": True})
    token = created.json()["trigger_token"]
    inv = client.post("/api/v1/triggers/alerts/invoke",
                      headers={"Authorization": "Bearer " + token, "X-Event-ID": "smoke-1"},
                      json={"status": "firing", "host": "web1", "title": "Disk full", "severity": "critical"})
    turn_id = inv.json()["turn_id"]
    with app.state.transaction() as db:
        db.execute("UPDATE role_turns SET state='completed',final_text=?,updated_at=? WHERE id=?",
                   ("根因：/var 日志暴涨；建议：清理并加轮转。", time.time(), turn_id))

    created = alert_notify.collect_conclusions(app.state.transaction, app.state.audit)
    check("one conclusion queued", created == 1)
    row = client.get("/api/v1/alert-notify/deliveries", headers=h).json()[0]
    check("body has a view/continue link", "查看 / 继续对话" in row["body"])
    check("link contains the turn anchor", "#turn=" + turn_id in row["body"])
    check("link points at the configured console", "http://10.0.0.5:8765/" in row["body"])

    # Clearing console_url removes the link on the next conclusion.
    client.put("/api/v1/alert-notify/config", headers=h, json={
        "url": "https://open.feishu.cn/open-apis/bot/v2/hook/smoke", "channel": "auto"})
    with app.state.transaction() as db:
        db.execute("UPDATE role_turns SET state='completed',final_text=?,updated_at=? WHERE id=?",
                   ("第二条结论。", time.time() + 1, turn_id))
        db.execute("DELETE FROM alert_notifications WHERE turn_id=?", (turn_id,))
    alert_notify.collect_conclusions(app.state.transaction, app.state.audit)
    row2 = client.get("/api/v1/alert-notify/deliveries", headers=h).json()[0]
    check("no link when console_url unset", "查看 / 继续对话" not in row2["body"])

    print("")
    if failures:
        print("SMOKE FAILED: " + ", ".join(failures))
        return 1
    print("ALL SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
