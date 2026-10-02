"""Explicit Linux host integration test. Reads secrets from files; never prints them."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request
import uuid


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:18765")
    p.add_argument("--token-file", required=True)
    p.add_argument("--agent-source", required=True)
    p.add_argument("--state-dir", required=True)
    p.add_argument("--user", required=True)
    args = p.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        raise SystemExit("Run this explicit host integration test as root on Linux")
    token = Path(args.token_file).read_text().strip()
    state = Path(args.state_dir).resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    run = uuid.uuid4().hex[:12]
    role = "smoke-" + run
    asset = "host-" + run
    missing = "aiops_missing_" + run
    report = []

    def api(method, path, data=None, expected=200):
        req = urllib.request.Request(args.url.rstrip("/") + path, method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                status, value = r.status, json.load(r)
        except urllib.error.HTTPError as e:
            status, value = e.code, json.load(e)
        assert status == expected, (path, status, expected)
        return value

    api("POST", "/api/v1/roles", {"id": role, "name": "Explicit host smoke test"}, 201)
    provisioned = api("POST", "/api/v1/assets", {"id": asset, "name": "Real Linux host test", "allowed_users": [args.user, missing], "notes": "Created by explicit integration test; no root account allowed"}, 201)
    conf = state / (run + "-agent.json")
    conf.write_text(json.dumps({"server_url": args.url, "allow_loopback_http": True, "asset_id": asset,
        "agent_token": provisioned["agent_token"], "allowed_users": [args.user, missing], "journal_dir": str(state / (run + "-journal"))}))
    conf.chmod(0o600)
    env = {**os.environ, "PYTHONPATH": args.agent_source, "PYTHONDONTWRITEBYTECODE": "1"}

    def once():
        subprocess.run([sys.executable, "-m", "ai_ops_agent.agent", "--config", str(conf), "--once"], env=env, check=True, timeout=30)

    def submit(user=args.user, mode="direct", command="id -un"):
        return api("POST", "/api/v1/tasks", {"role_id": role, "asset_id": asset, "execution_users": [user], "run_as": user,
            "command": command, "mode": mode, "idempotency_key": str(uuid.uuid4())}, 201)

    first = submit()
    once()
    result = api("GET", "/api/v1/tasks/" + first["id"])
    assert result["state"] == "succeeded" and result["result"]["stdout"].strip() == args.user
    report.append({"case": "actual_host_native_user", "passed": True, "task_id": first["id"], "actual_user": args.user})

    bad = submit(user=missing)
    once()
    result = api("GET", "/api/v1/tasks/" + bad["id"])
    assert result["state"] == "failed" and result["result"]["error_code"] == "USER_NOT_FOUND"
    report.append({"case": "missing_user_no_fallback", "passed": True, "task_id": bad["id"]})

    approval = submit(mode="confirm")
    once()
    assert api("GET", "/api/v1/tasks/" + approval["id"])["state"] == "awaiting_approval"
    api("POST", "/api/v1/tasks/" + approval["id"] + "/approve")
    once()
    assert api("GET", "/api/v1/tasks/" + approval["id"])["state"] == "succeeded"
    report.append({"case": "approval_gates_execution", "passed": True, "task_id": approval["id"]})

    api("POST", "/api/v1/tasks", {"role_id": role, "asset_id": asset, "execution_users": ["root"], "run_as": "root",
        "command": "id -un", "idempotency_key": str(uuid.uuid4())}, 403)
    report.append({"case": "unconfigured_root_rejected", "passed": True})

    events = api("GET", "/api/v1/audit?limit=500")
    assert any(e["entity_id"] == first["id"] and e["event"] == "task.result" for e in events)
    assert provisioned["agent_token"] not in json.dumps(events)
    report.append({"case": "durable_audit_without_agent_token", "passed": True})
    output = {"passed": True, "role_id": role, "asset_id": asset, "cases": report}
    (state / "latest-report.json").write_text(json.dumps(output, indent=2))
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
