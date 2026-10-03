import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ai_ops import scrubbing
from ai_ops.app import create_app
from test_control import env, submit, claim, finish, ADMIN


def test_redacts_named_secret_assignments():
    text = "db_password=hunter2\nAPI_KEY: sk-abcdefghijklmnopqrstuvwx\nkeep this"
    out = scrubbing.redact_text(text)
    assert "hunter2" not in out and "sk-abcdefghijklmnopqrstuvwx" not in out
    assert "[REDACTED]" in out and "keep this" in out
    # The field name survives so the reader knows what was hidden.
    assert "db_password" in out and "API_KEY" in out


def test_redacts_authorization_header():
    out = scrubbing.redact_text("Authorization: Bearer abcdef0123456789XYZ")
    assert "abcdef0123456789XYZ" not in out and "Authorization" in out


def test_redacts_url_credentials():
    out = scrubbing.redact_text("connecting to https://admin:s3cr3t@db.internal/x")
    assert "s3cr3t" not in out and "https://admin:" in out and "@db.internal" in out


def test_redacts_private_key_block():
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAbbbbCCCC\n-----END OPENSSH PRIVATE KEY-----"
    out = scrubbing.redact_text("key follows:\n" + pem + "\ndone")
    assert "AAAAbbbbCCCC" not in out and "BEGIN OPENSSH PRIVATE KEY" not in out
    assert "done" in out


def test_redacts_provider_shaped_tokens():
    for token in ("ghp_" + "a" * 30, "AKIA" + "A" * 16, "AIza" + "b" * 30):
        out = scrubbing.redact_text("token=%s end" % token)
        assert token not in out


def test_redacts_json_secret_fields():
    payload = json.dumps({"user": "bob", "password": "p@ssw0rd!", "token": "abcdef0123456789"})
    out = scrubbing.redact_text(payload)
    assert "p@ssw0rd!" not in out and "abcdef0123456789" not in out
    assert "bob" in out


def test_redacts_operator_declared_literals():
    out = scrubbing.redact_text("the value is SuperSecretValue99 here", literals=["SuperSecretValue99"])
    assert "SuperSecretValue99" not in out and "[REDACTED]" in out


def test_redact_result_touches_only_output_fields():
    result = {"status": "succeeded", "exit_code": 0, "error_code": None,
              "stdout": "password=topsecret", "stderr": "Authorization: Bearer deadbeefcafe00"}
    out = scrubbing.redact_result(result)
    assert out["status"] == "succeeded" and out["exit_code"] == 0
    assert "topsecret" not in out["stdout"] and "deadbeefcafe00" not in out["stderr"]


def test_find_secret_kinds_flags_without_altering():
    kinds = scrubbing.find_secret_kinds("Authorization: Bearer abcdef01234567")
    assert "authorization_header" in kinds


def test_clean_text_is_unchanged():
    text = "total 12\ndrwxr-xr-x 2 root root 4096 file.txt\n"
    assert scrubbing.redact_text(text) == text


def test_result_endpoint_scrubs_inline_output(env):
    c, h, tokens, path = env
    submit(env, key="request-001")
    task = claim(env).json()["task"]
    result = {"claim_id": task["claim_id"], "status": "succeeded", "exit_code": 0,
              "stdout": "export API_KEY=sk-abcdefghijklmnopqrstuvwx\nok",
              "stderr": "Authorization: Bearer abcdef0123456789XYZ"}
    response = c.post("/api/v1/agents/a/tasks/" + task["id"] + "/result", headers=tokens["a"], json=result)
    assert response.status_code == 200, response.text
    stored = c.get("/api/v1/tasks/" + task["id"], headers=h).json()["result"]
    assert "sk-abcdefghijklmnopqrstuvwx" not in stored["stdout"]
    assert "abcdef0123456789XYZ" not in stored["stderr"]
    assert "[REDACTED]" in stored["stdout"]


def test_result_endpoint_scrubs_audit_details(env):
    c, h, tokens, path = env
    submit(env, key="request-001")
    task = claim(env).json()["task"]
    c.post("/api/v1/agents/a/tasks/" + task["id"] + "/result", headers=tokens["a"], json={
        "claim_id": task["claim_id"], "status": "succeeded", "exit_code": 0,
        "stdout": "token=ghp_" + "a" * 30})
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT details FROM audit WHERE event='task.result'").fetchone()
    assert "ghp_" + "a" * 30 not in row[0]


def test_retry_of_a_redacted_result_is_idempotent(env):
    # A command is retried by the agent with the same (secret-bearing) payload;
    # the second identical submit must be recognised as a duplicate, not a 409.
    c, h, tokens, _ = env
    submit(env, key="request-001")
    task = claim(env).json()["task"]
    payload = {"claim_id": task["claim_id"], "status": "succeeded", "exit_code": 0,
               "stdout": "password=supersecret"}
    url = "/api/v1/agents/a/tasks/" + task["id"] + "/result"
    assert c.post(url, headers=tokens["a"], json=payload).json() == {"accepted": True, "duplicate": False}
    assert c.post(url, headers=tokens["a"], json=payload).json() == {"accepted": True, "duplicate": True}
