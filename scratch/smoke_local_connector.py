"""Real end-to-end smoke for the local connector: start the app, submit a task
against the control host asset, run the worker executor, and verify the stored
result. Confirms the local transport behaves like any other asset."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient  # noqa: E402
from ai_ops.app import create_app  # noqa: E402
from ai_ops import connector_local  # noqa: E402

ADMIN = "smoke-admin-token-not-real-0000000000000000"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + ("  " + detail if detail else ""))


def main():
    path = os.path.join(tempfile.mkdtemp(), "control.db")
    c = TestClient(create_app(path, ADMIN))
    h = {"Authorization": "Bearer " + ADMIN}
    c.post("/api/v1/roles", headers=h, json={"id": "ops", "name": "Ops"})

    assets = c.get("/api/v1/assets", headers=h).json()
    local = [a for a in assets if a["connection_type"] == "local"]
    check("control host is registered by default", len(local) == 1)
    aid = local[0]["id"] if local else None
    check("control host asset id is the reserved id", aid == connector_local.LOCAL_ASSET_ID, str(aid))

    cfg = c.get("/api/v1/local-connector", headers=h).json()
    run_as = cfg["run_as"]
    check("run_as reported", bool(run_as), str(run_as))

    # Submit a real read-only command and execute it via the worker executor.
    cmd = "echo aiops-local" if os.name == "posix" else "echo aiops-local"
    r = c.post("/api/v1/tasks", headers=h, json={
        "role_id": "ops", "asset_id": aid, "execution_users": [run_as], "run_as": run_as,
        "command": cmd, "mode": "direct", "idempotency_key": "smoke-local-0001"})
    check("task accepted", r.status_code == 201, r.text[:120])
    task = r.json()

    executor = connector_local.LocalExecutor(_transaction_for(path), _audit_for(path))
    claimed = executor.claim(aid)
    check("task claimed by local executor", claimed is not None)
    result = executor.run(aid, claimed, None)
    check("task succeeded", result["status"] == "succeeded", str(result.get("stderr"))[:120])
    check("stdout captured", "aiops-local" in result["stdout"], result["stdout"].strip())

    stored = c.get("/api/v1/tasks/" + task["id"], headers=h).json()
    check("stored state succeeded", stored["state"] == "succeeded")
    check("lease closed", stored["lease"]["closed"] is True)

    events = [e["event"] for e in c.get("/api/v1/audit?limit=300", headers=h).json()]
    check("claim audited", "task.claimed" in events)
    check("result audited", "task.result" in events)

    # Disable hides the asset but preserves history.
    c.post("/api/v1/local-connector/disable", headers=h)
    listed = c.get("/api/v1/assets", headers=h).json()
    check("disabled local hidden from list", not [a for a in listed if a["connection_type"] == "local"])
    check("history survives disable", c.get("/api/v1/tasks/" + task["id"], headers=h).json()["state"] == "succeeded")
    enabled = c.post("/api/v1/local-connector/enable", headers=h).json()
    check("re-enabled", enabled["enabled"] is True)

    # Fail-closed credential check.
    bad = c.post("/api/v1/agents/%s/claim" % aid, headers={"Authorization": "Bearer " + "x" * 40},
                 json={"protocol_version": "1.1"})
    check("local asset rejects arbitrary token", bad.status_code in (401, 403), str(bad.status_code))

    print()
    failed = [n for n, ok, _ in results if not ok]
    print("%d/%d checks passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


def _transaction_for(path):
    import contextlib
    import sqlite3

    @contextlib.contextmanager
    def transaction():
        db = sqlite3.connect(path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
    return transaction


def _audit_for(path):
    import json

    def audit(db, event, entity_id, actor, details):
        db.execute("INSERT INTO audit(event,entity_id,actor,details,created_at) VALUES(?,?,?,?,?)",
                   (event, entity_id, actor, json.dumps(details, ensure_ascii=False), time.time()))
    return audit


if __name__ == "__main__":
    raise SystemExit(main())
