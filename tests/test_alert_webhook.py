"""Alert result fan-out: channel adapters, SSRF floor, dedupe, resolved, retries.

Design: docs/alert-webhook.md (2026-10-04). The push is a CONCLUSION, not the
raw alarm, and the SSRF refusal is a safety floor rather than a feature.
"""
import json
import tempfile
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from ai_ops.app import create_app
from ai_ops import alert_notify

ADMIN = "a" * 40


def make_client():
    return TestClient(create_app(str(Path(tempfile.mkdtemp()) / "control.db"), ADMIN, "test-model"))


def headers():
    return {"Authorization": "Bearer " + ADMIN}


# ---- channel classification -------------------------------------------------

def test_channel_classified_by_host_with_generic_fallback():
    assert alert_notify.classify_channel("https://open.feishu.cn/open-apis/bot/v2/hook/x") == "feishu"
    assert alert_notify.classify_channel("https://open.larksuite.com/open-apis/bot/v2/hook/x") == "feishu"
    assert alert_notify.classify_channel("https://oapi.dingtalk.com/robot/send?access_token=x") == "dingtalk"
    assert alert_notify.classify_channel("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x") == "wecom"
    assert alert_notify.classify_channel("https://hooks.slack.com/services/x") == "slack"
    assert alert_notify.classify_channel("https://discord.com/api/webhooks/x/y") == "discord"
    assert alert_notify.classify_channel("https://example.com/hook") == "generic"


def test_payload_shape_matches_channel():
    feishu = alert_notify.build_payload("feishu", "T", "B")
    assert feishu["msg_type"] == "text" and "T" in feishu["content"]["text"]
    ding = alert_notify.build_payload("dingtalk", "T", "B")
    assert ding["msgtype"] == "markdown" and ding["markdown"]["title"] == "T"
    wecom = alert_notify.build_payload("wecom", "T", "B")
    assert wecom["msgtype"] == "markdown" and "T" in wecom["markdown"]["content"]
    slack = alert_notify.build_payload("slack", "T", "B")
    assert "T" in slack["text"]
    disc = alert_notify.build_payload("discord", "T", "B")
    assert "T" in disc["content"]
    generic = alert_notify.build_payload("generic", "T", "B")
    assert generic["source"] == "ai-ops" and generic["title"] == "T"


# ---- SSRF floor -------------------------------------------------------------

def test_ssrf_refuses_loopback_private_linklocal_and_metadata():
    for bad in ("http://127.0.0.1/hook", "http://localhost/hook", "http://10.0.0.5/hook",
                "http://192.168.1.1/hook", "http://172.16.0.1/hook",
                "http://169.254.169.254/latest/meta-data/", "http://[::1]/hook",
                "http://0.0.0.0/hook"):
        try:
            alert_notify.validate_url(bad)
        except ValueError as e:
            assert str(e) == "SSRF_BLOCKED_ADDRESS" or str(e).startswith("SSRF_"), bad
        else:
            raise AssertionError("expected refusal for " + bad)


def test_ssrf_refuses_non_http_scheme_and_embedded_credentials():
    for bad, reason in (("file:///etc/passwd", "SSRF_UNSUPPORTED_SCHEME"),
                        ("ftp://example.com/x", "SSRF_UNSUPPORTED_SCHEME"),
                        ("http://user:pass@example.com/hook", "SSRF_EMBEDDED_CREDENTIALS")):
        try:
            alert_notify.validate_url(bad)
        except ValueError as e:
            assert str(e) == reason, (bad, str(e))
        else:
            raise AssertionError("expected refusal for " + bad)


def test_ssrf_refuses_name_that_resolves_internal():
    # A public-looking name that resolves to a private address must still be refused.
    fake = [(2, 1, 6, "", ("10.1.2.3", 0))]
    with mock.patch("ai_ops.alert_notify.socket.getaddrinfo", return_value=fake):
        try:
            alert_notify.validate_url("http://internal.example.com/hook")
        except ValueError as e:
            assert str(e) == "SSRF_BLOCKED_ADDRESS"
        else:
            raise AssertionError("expected refusal for a name resolving to a private IP")


def test_config_rejects_ssrf_url_over_api():
    client = make_client()
    r = client.put("/api/v1/alert-notify/config", headers=headers(),
                   json={"url": "http://169.254.169.254/hook", "channel": "auto"})
    assert r.status_code == 422
    assert "SSRF" in r.text


# ---- config + redaction -----------------------------------------------------

def test_config_roundtrip_redacts_secret_query():
    client = make_client()
    url = "https://oapi.dingtalk.com/robot/send?access_token=supersecrettokenvalue"
    r = client.put("/api/v1/alert-notify/config", headers=headers(),
                   json={"url": url, "channel": "auto"})
    assert r.status_code == 200
    assert r.json()["channel"] == "dingtalk"
    assert "supersecrettokenvalue" not in r.text
    assert "[REDACTED]" in r.json()["url"]
    got = client.get("/api/v1/alert-notify/config", headers=headers()).json()
    assert got["configured"] is True and "supersecrettokenvalue" not in json.dumps(got)


def test_clear_config_disables():
    client = make_client()
    client.put("/api/v1/alert-notify/config", headers=headers(),
               json={"url": "https://hooks.slack.com/services/x", "channel": "auto"})
    out = client.delete("/api/v1/alert-notify/config", headers=headers()).json()
    assert out["enabled"] is False
    assert client.get("/api/v1/alert-notify/config", headers=headers()).json()["enabled"] is False


def test_test_push_delivers_and_reports_failure():
    client = make_client()
    client.put("/api/v1/alert-notify/config", headers=headers(),
               json={"url": "https://hooks.slack.com/services/x", "channel": "auto"})
    with mock.patch("ai_ops.alert_notify.post_once", return_value=None) as send:
        r = client.post("/api/v1/alert-notify/test", headers=headers())
        assert r.status_code == 200 and r.json()["sent"] is True
        assert send.called
    with mock.patch("ai_ops.alert_notify.post_once", side_effect=RuntimeError("boom")):
        r = client.post("/api/v1/alert-notify/test", headers=headers())
        assert r.status_code == 502 and "RuntimeError" in r.text


# ---- conclusion fan-out via the alarm path ----------------------------------

def _setup_alarm(client, headers, status_value="firing"):
    client.post("/api/v1/roles", headers=headers, json={"id": "ops", "name": "Ops"})
    created = client.post("/api/v1/custom-tasks", headers=headers, json={
        "id": "alerts", "name": "Alerts", "kind": "trigger", "role_id": "ops",
        "prompt": "诊断告警", "execution_users": ["ops_read"], "mode": "readonly", "enabled": True})
    token = created.json()["trigger_token"]
    body = {"status": status_value, "host": "web1", "title": "Disk full", "severity": "critical"}
    r = client.post("/api/v1/triggers/alerts/invoke",
                    headers={"Authorization": "Bearer " + token, "X-Event-ID": "evt-1"}, json=body)
    assert r.status_code == 202
    return r.json()["turn_id"]


def _complete_turn(client, turn_id, text):
    # Drive the turn to completed the way the model worker would.
    app = client.app
    with app.state.transaction() as db:
        import time
        db.execute("UPDATE role_turns SET state='completed',final_text=?,updated_at=? WHERE id=?",
                   (text, time.time(), turn_id))


def test_completed_alarm_turn_becomes_one_push_row():
    client = make_client()
    h = headers()
    client.put("/api/v1/alert-notify/config", headers=h,
               json={"url": "https://open.feishu.cn/open-apis/bot/v2/hook/x", "channel": "auto"})
    turn_id = _setup_alarm(client, h)
    _complete_turn(client, turn_id, "根因：/var 日志暴涨；建议：清理旧日志并加轮转。")
    app = client.app
    created = alert_notify.collect_conclusions(app.state.transaction, app.state.audit)
    assert created == 1
    rows = client.get("/api/v1/alert-notify/deliveries", headers=h).json()
    assert len(rows) == 1
    row = rows[0]
    assert row["turn_id"] == turn_id
    assert row["title"].startswith("[已分析]")
    assert "根因" in row["body"] and "仍在告警中" in row["body"]
    assert row["channel"] == "feishu" and row["status"] == "pending"


def test_dedupe_does_not_push_twice_and_resolved_is_reflected():
    client = make_client()
    h = headers()
    client.put("/api/v1/alert-notify/config", headers=h,
               json={"url": "https://oapi.dingtalk.com/robot/send?access_token=abcdefg", "channel": "auto"})
    turn_id = _setup_alarm(client, h, status_value="resolved")
    _complete_turn(client, turn_id, "该问题在分析期间已自动恢复。")
    app = client.app
    assert alert_notify.collect_conclusions(app.state.transaction, app.state.audit) == 1
    # A second pass must not create a duplicate push for the same turn.
    assert alert_notify.collect_conclusions(app.state.transaction, app.state.audit) == 0
    rows = client.get("/api/v1/alert-notify/deliveries", headers=h).json()
    assert len(rows) == 1
    assert rows[0]["alarm_state"] == "resolved"
    assert "已恢复" in rows[0]["body"]


def test_no_collect_when_config_disabled():
    client = make_client()
    h = headers()
    client.put("/api/v1/alert-notify/config", headers=h, json={"url": "https://example.com/hook", "channel": "auto"})
    client.delete("/api/v1/alert-notify/config", headers=h)
    turn_id = _setup_alarm(client, h)
    _complete_turn(client, turn_id, "x")
    app = client.app
    assert alert_notify.collect_conclusions(app.state.transaction, app.state.audit) == 0


# ---- delivery + retry behaviour ---------------------------------------------

def test_delivery_success_marks_delivered():
    client = make_client()
    h = headers()
    client.put("/api/v1/alert-notify/config", headers=h, json={"url": "https://example.com/hook", "channel": "auto"})
    app = client.app
    with app.state.transaction() as db:
        alert_notify.enqueue_notification(db, turn_id="t1", dedupe_key="turn:t1",
                                          url="https://example.com/hook", channel="generic",
                                          title="T", body="B")
    import ai_ops.app  # noqa: F401
    # Patch only the network boundary.
    with mock.patch("ai_ops.alert_notify.post_once", return_value=None):
        assert alert_notify.deliver_pending(app.state.transaction, app.state.audit) == 1
    rows = client.get("/api/v1/alert-notify/deliveries", headers=h).json()
    assert rows[0]["status"] == "delivered" and rows[0]["delivered_at"] is not None


def test_delivery_retries_then_fails_without_leaking_secret():
    client = make_client()
    h = headers()
    secret_url = "https://oapi.dingtalk.com/robot/send?access_token=topsecrettoken123"
    client.put("/api/v1/alert-notify/config", headers=h, json={"url": secret_url, "channel": "auto"})
    app = client.app
    with app.state.transaction() as db:
        alert_notify.enqueue_notification(db, turn_id="t2", dedupe_key="turn:t2",
                                          url=secret_url, channel="dingtalk", title="T", body="B")
    with mock.patch("ai_ops.alert_notify.post_once", side_effect=RuntimeError("conn")):
        # Exhaust the bounded retry budget, advancing the clock past each backoff.
        import time
        clock = time.time()
        for _ in range(alert_notify.MAX_ATTEMPTS):
            clock += 10_000
            alert_notify.deliver_pending(app.state.transaction, app.state.audit, now=clock)
    rows = client.get("/api/v1/alert-notify/deliveries", headers=h).json()
    assert rows[0]["status"] == "failed"
    assert rows[0]["last_error"] == "RuntimeError"
    assert "topsecrettoken123" not in json.dumps(rows)


def test_ssrf_failure_is_permanent_not_retried():
    client = make_client()
    app = client.app
    with app.state.transaction() as db:
        alert_notify.enqueue_notification(db, turn_id="t3", dedupe_key="turn:t3",
                                          url="http://10.0.0.9/hook", channel="generic",
                                          title="T", body="B")
    # post_once validates and raises a permanent SSRF_* refusal.
    with mock.patch("ai_ops.alert_notify._host_is_blocked", return_value=True):
        alert_notify.deliver_pending(app.state.transaction, app.state.audit)
    with app.state.transaction() as db:
        row = db.execute("SELECT status,last_error FROM alert_notifications WHERE turn_id='t3'").fetchone()
    assert row["status"] == "failed" and row["last_error"].startswith("SSRF_")


# ---- console deep link in the pushed conclusion ----------------------------

def test_console_link_builder_appends_anchor_and_keeps_fragment():
    assert alert_notify.build_console_link(None, "t1") is None
    assert alert_notify.build_console_link("", "t1") is None
    assert alert_notify.build_console_link("https://ops.example.com/", "abc") == \
        "https://ops.example.com/#turn=abc"
    # An existing fragment is preserved, not truncated.
    assert alert_notify.build_console_link("https://ops.example.com/#panel=alarms", "abc") == \
        "https://ops.example.com/#panel=alarms&turn=abc"
    # A turn id with a space/plus is percent-encoded so the anchor stays parseable.
    assert alert_notify.build_console_link("https://h", "a b") == "https://h#turn=a%20b"


def test_console_url_validation_allows_private_but_rejects_bad_scheme_and_userinfo():
    # A private/VPN console address is the normal self-hosted case; allowed.
    assert alert_notify.validate_console_url("http://10.0.0.5:8765/")
    import pytest
    with pytest.raises(ValueError):
        alert_notify.validate_console_url("file:///etc/passwd")
    with pytest.raises(ValueError):
        alert_notify.validate_console_url("https://user:pw@ops.example.com/")


def test_config_roundtrip_console_url_and_rejects_bad_one():
    client = make_client()
    h = headers()
    r = client.put("/api/v1/alert-notify/config", headers=h, json={
        "url": "https://open.feishu.cn/open-apis/bot/v2/hook/x", "channel": "auto",
        "console_url": "https://ops.example.com"})
    assert r.status_code == 200 and r.json()["console_url"] == "https://ops.example.com"
    got = client.get("/api/v1/alert-notify/config", headers=h).json()
    assert got["console_url"] == "https://ops.example.com"
    bad = client.put("/api/v1/alert-notify/config", headers=h, json={
        "url": "https://open.feishu.cn/open-apis/bot/v2/hook/x", "channel": "auto",
        "console_url": "ftp://ops.example.com"})
    assert bad.status_code == 422


def test_pushed_conclusion_carries_console_link_when_configured():
    client = make_client()
    h = headers()
    client.put("/api/v1/alert-notify/config", headers=h, json={
        "url": "https://open.feishu.cn/open-apis/bot/v2/hook/x", "channel": "auto",
        "console_url": "https://ops.example.com"})
    turn_id = _setup_alarm(client, h)
    _complete_turn(client, turn_id, "根因：磁盘写满。")
    app = client.app
    assert alert_notify.collect_conclusions(app.state.transaction, app.state.audit) == 1
    row = client.get("/api/v1/alert-notify/deliveries", headers=h).json()[0]
    assert "查看 / 继续对话" in row["body"]
    assert "#turn=" + turn_id in row["body"]
    assert "https://ops.example.com" in row["body"]


def test_pushed_conclusion_has_no_link_when_console_url_unset():
    client = make_client()
    h = headers()
    client.put("/api/v1/alert-notify/config", headers=h,
               json={"url": "https://open.feishu.cn/open-apis/bot/v2/hook/x", "channel": "auto"})
    turn_id = _setup_alarm(client, h)
    _complete_turn(client, turn_id, "结论。")
    app = client.app
    assert alert_notify.collect_conclusions(app.state.transaction, app.state.audit) == 1
    row = client.get("/api/v1/alert-notify/deliveries", headers=h).json()[0]
    assert "查看 / 继续对话" not in row["body"]
