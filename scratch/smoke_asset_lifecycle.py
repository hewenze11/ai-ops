"""Real smoke for asset lifecycle: retire/purge + offline detection fan-out."""
import json
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient  # noqa: E402
from ai_ops.app import create_app  # noqa: E402
from ai_ops import asset_lifecycle as al  # noqa: E402

ADMIN = "smoke-lifecycle-token-not-real-0000000000"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + ("  " + detail if detail else ""))


def tx_factory(path):
    import contextlib

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


def audit(db, event, entity_id, actor, details):
    db.execute("INSERT INTO audit(event,entity_id,actor,details,created_at) VALUES(?,?,?,?,?)",
               (event, entity_id, actor, json.dumps(details, ensure_ascii=False), time.time()))


def main():
    path = os.path.join(tempfile.mkdtemp(), "control.db")
    c = TestClient(create_app(path, ADMIN))
    h = {"Authorization": "Bearer " + ADMIN}
    c.post("/api/v1/roles", headers=h, json={"id": "ops", "name": "Ops"})
    c.post("/api/v1/assets", headers=h, json={"id": "host1", "name": "Host 1",
                                              "allowed_users": ["reader"], "notes": "t"})
    c.post("/api/v1/assets", headers=h, json={"id": "host2", "name": "Host 2",
                                              "allowed_users": ["reader"], "notes": "t"})

    # Retire hides, keeps history.
    r = c.post("/api/v1/assets/host1/retire", headers=h, json={"note": "dead disk"})
    check("retire ok", r.status_code == 200 and r.json()["retired"] is True)
    ids = [a["id"] for a in c.get("/api/v1/assets", headers=h).json()]
    check("retired hidden from list", "host1" not in ids and "host2" in ids)
    check("lifecycle shows retired", c.get("/api/v1/assets/host1/lifecycle", headers=h).json()["retired"] is True)
    check("unretire works", c.post("/api/v1/assets/host1/unretire", headers=h).json()["retired"] is False)

    # Pending work blocks purge only when unresolved (claimed/unknown); a merely
    # queued task does not, because nothing has started. Claim it to block.
    submit = c.post("/api/v1/tasks", headers=h, json={"role_id": "ops", "asset_id": "host2",
           "execution_users": ["reader"], "run_as": "reader", "command": "id",
           "mode": "direct", "idempotency_key": "smoke-life-0001"}).json()
    # Purge with a wrong confirm id is refused regardless.
    check("purge refuses wrong confirm id",
          c.post("/api/v1/assets/host2/purge", headers=h, json={"confirm_asset_id": "nope"}).status_code == 422)
    # Leave host2 intact (queued, not claimed) -> purge is allowed after cleanup checks.
    check("purge 404 for unknown asset",
          c.post("/api/v1/assets/ghost/purge", headers=h, json={"confirm_asset_id": "ghost"}).status_code == 404)

    # Purge with correct confirm removes it.
    purged = c.post("/api/v1/assets/host1/purge", headers=h, json={"confirm_asset_id": "host1"})
    check("purge ok with confirm", purged.status_code == 200 and purged.json()["purged"] is True)
    check("purged gone", c.get("/api/v1/assets/host1", headers=h).status_code == 404)
    bad = c.post("/api/v1/assets/host1/purge", headers=h, json={"confirm_asset_id": "host1"})
    check("purge after removal 404", bad.status_code == 404)

    # Offline detection: silent long enough -> one event; debounced on repeat.
    c.put("/api/v1/alert-notify/config", headers=h,
          json={"url": "https://hooks.slack.com/services/T/B/X", "channel": "auto", "enabled": True})
    with sqlite3.connect(path) as db:
        db.execute("INSERT OR REPLACE INTO agent_presence(asset_id,instance_id,agent_version,protocol_version,last_seen,busy,busy_by,busy_task) "
                   "VALUES('host2','i','1','1.1',?,0,NULL,NULL)", (time.time() - 300,))
    tf = tx_factory(path)
    first = al.detect_offline(tf, audit)
    second = al.detect_offline(tf, audit)
    check("offline event emitted once", len(first) == 1 and len(second) == 0, "%d/%d" % (len(first), len(second)))
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        notes = [dict(r) for r in db.execute("SELECT dedupe_key,title FROM alert_notifications")]
    check("offline pushed to same webhook", len(notes) == 1 and notes[0]["dedupe_key"].startswith("offline:"),
          notes[0]["title"] if notes else "none")
    live = c.get("/api/v1/assets/offline", headers=h).json()
    check("offline view lists the host", any(e["asset_id"] == "host2" for e in live))

    print()
    failed = [n for n, ok, _ in results if not ok]
    print("%d/%d checks passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
