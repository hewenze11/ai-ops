"""Real-server smoke test: a role turn exposes a derived '分析步骤' (steps) list."""
import json
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
    tmp = tempfile.mkdtemp(prefix="aiops-steps-")
    os.environ["AI_OPS_DB"] = os.path.join(tmp, "control.db")
    from fastapi.testclient import TestClient
    from ai_ops.app import create_app

    app = create_app(os.path.join(tmp, "control.db"), ADMIN)
    client = TestClient(app)
    h = {"Authorization": "Bearer " + ADMIN}
    client.post("/api/v1/roles", headers=h, json={"id": "ops", "name": "Ops"})

    created = client.post("/api/v1/roles/ops/messages", headers=h, json={
        "text": "看下磁盘", "execution_users": ["ops_read"], "mode": "confirm",
        "idempotency_key": "smoke-steps-1"})
    turn_id = created.json()["turn_id"]

    # Simulate what the model worker records: a thought, a proposed command, its result.
    messages = [
        {"role": "assistant", "content": "先检查根分区使用率。", "tool_calls": [
            {"id": "c1", "type": "function", "function": {
                "name": "execute_command",
                "arguments": json.dumps({"asset_id": "control-host-local", "run_as": "ops_read",
                                         "command": "df -h /"})}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(
            {"status": "succeeded", "exit_code": 0, "stdout": "/dev/sda1 40G 38G 2G 95% /\n"})},
        {"role": "assistant", "content": "根分区已到 95%，需要清理日志。"},
    ]
    with app.state.transaction() as db:
        db.execute("UPDATE role_turns SET messages=?,state='completed',final_text=?,updated_at=? WHERE id=?",
                   (json.dumps(messages, ensure_ascii=False), "根分区 95%，建议清理日志。", time.time(), turn_id))

    got = client.get("/api/v1/turns/" + turn_id, headers=h).json()
    check("turn exposes a steps list", isinstance(got.get("steps"), list))
    kinds = [s["kind"] for s in got["steps"]]
    check("steps are thought->command->thought", kinds == ["thought", "command", "thought"])
    command = got["steps"][1]
    check("command step carries the command", command["command"] == "df -h /")
    check("command step is marked succeeded", command["status"] == "succeeded")
    check("command step carries an output excerpt", "95%" in (command["output_excerpt"] or ""))
    check("thought step carries the model's own words", got["steps"][0]["text"].startswith("先检查"))

    # An unexecuted (proposed) command must NOT claim it ran.
    messages2 = [{"role": "assistant", "content": "要重启服务。", "tool_calls": [
        {"id": "c2", "type": "function", "function": {
            "name": "execute_command", "arguments": json.dumps({"command": "systemctl restart x"})}}]}]
    with app.state.transaction() as db:
        db.execute("UPDATE role_turns SET messages=?,state='awaiting_approval',updated_at=? WHERE id=?",
                   (json.dumps(messages2, ensure_ascii=False), time.time() + 1, turn_id))
    got2 = client.get("/api/v1/turns/" + turn_id, headers=h).json()
    cmd2 = [s for s in got2["steps"] if s["kind"] == "command"][0]
    check("unexecuted command stays 'proposed'", cmd2["status"] == "proposed")
    check("unexecuted command has no output", cmd2["output_excerpt"] is None)

    print("")
    if failures:
        print("SMOKE FAILED: " + ", ".join(failures))
        return 1
    print("ALL SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
