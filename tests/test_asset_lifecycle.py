import json
import sqlite3
import time

import pytest
from ai_ops import asset_lifecycle as al

from test_control import env, submit, claim, finish, ADMIN


def _retire(c, h, aid, note="decommissioned"):
    return c.post("/api/v1/assets/%s/retire" % aid, headers=h, json={"note": note})


def test_retire_hides_but_keeps_history(env):
    c, h, _, _ = env
    # Give asset "a" a completed task so we have history to preserve.
    task = submit(env).json()
    claimed = claim(env).json()["task"]
    finish(env, claimed)
    assert _retire(c, h, "a").json()["retired"] is True
    listed = c.get("/api/v1/assets", headers=h).json()
    assert "a" not in [a["id"] for a in listed]
    # History survives: the task is still readable.
    assert c.get("/api/v1/tasks/" + task["id"], headers=h).json()["state"] == "succeeded"
    life = c.get("/api/v1/assets/a/lifecycle", headers=h).json()
    assert life["retired"] is True and life["retire"]["retire_note"] == "decommissioned"
    # Retiring twice conflicts.
    assert _retire(c, h, "a").status_code == 409


def test_unretire_restores_asset(env):
    c, h, _, _ = env
    _retire(c, h, "a")
    assert c.post("/api/v1/assets/a/unretire", headers=h).json()["retired"] is False
    assert "a" in [a["id"] for a in c.get("/api/v1/assets", headers=h).json()]
    assert c.post("/api/v1/assets/a/unretire", headers=h).status_code == 409


def test_purge_requires_confirmation(env):
    c, h, _, _ = env
    bad = c.post("/api/v1/assets/a/purge", headers=h, json={"confirm_asset_id": "b"})
    assert bad.status_code == 422
    assert "a" in [a["id"] for a in c.get("/api/v1/assets", headers=h).json()]


def test_purge_removes_asset_and_dependents(env):
    c, h, _, path = env
    task = submit(env).json()
    claimed = claim(env).json()["task"]
    finish(env, claimed)
    resp = c.post("/api/v1/assets/a/purge", headers=h, json={"confirm_asset_id": "a"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["purged"] is True
    assert "a" not in [a["id"] for a in c.get("/api/v1/assets", headers=h).json()]
    assert c.get("/api/v1/tasks/" + task["id"], headers=h).status_code == 404
    # The other asset is untouched.
    assert c.get("/api/v1/assets/b", headers=h).status_code == 200
    # Audit records the purge.
    events = [e["event"] for e in c.get("/api/v1/audit?limit=200", headers=h).json()]
    assert "asset.purged" in events


def test_purge_refused_while_execution_unresolved(env):
    c, h, _, _ = env
    submit(env)
    claim(env)  # leaves the task 'claimed'
    resp = c.post("/api/v1/assets/a/purge", headers=h, json={"confirm_asset_id": "a"})
    assert resp.status_code == 409
    assert "a" in [a["id"] for a in c.get("/api/v1/assets", headers=h).json()]


def test_offline_detection_emits_event(env):
    c, h, tokens, path = env
    now = time.time()
    # Register a presence for asset "a" that is long past the TTL and debounce.
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO agent_presence(asset_id,instance_id,agent_version,protocol_version,last_seen,busy,busy_by,busy_task) "
                   "VALUES('a','i','1','1.1',?,0,NULL,NULL)", (now - 200,))
    created = al.detect_offline(_tx(path), _audit)
    assert len(created) == 1 and created[0]["asset_id"] == "a"
    events = c.get("/api/v1/assets/offline", headers=h).json()
    assert any(e["asset_id"] == "a" for e in events)


def test_offline_debounce_prevents_spam(env):
    c, h, _, path = env
    now = time.time()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO agent_presence(asset_id,instance_id,agent_version,protocol_version,last_seen,busy,busy_by,busy_task) "
                   "VALUES('a','i','1','1.1',?,0,NULL,NULL)", (now - 200,))
    first = al.detect_offline(_tx(path), _audit)
    second = al.detect_offline(_tx(path), _audit)
    assert len(first) == 1 and len(second) == 0, "a silent host must not re-alert every tick"


def test_offline_short_gap_does_not_emit(env):
    _, _, _, path = env
    now = time.time()
    # Silent 45s: past the 30s TTL but inside the 90s debounce -> no event yet.
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO agent_presence(asset_id,instance_id,agent_version,protocol_version,last_seen,busy,busy_by,busy_task) "
                   "VALUES('a','i','1','1.1',?,0,NULL,NULL)", (now - 45,))
    assert al.detect_offline(_tx(path), _audit) == []


def test_recovered_host_resets_debounce(env):
    _, _, _, path = env
    now = time.time()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO agent_presence(asset_id,instance_id,agent_version,protocol_version,last_seen,busy,busy_by,busy_task) "
                   "VALUES('a','i','1','1.1',?,0,NULL,NULL)", (now - 200,))
    al.detect_offline(_tx(path), _audit)
    # Host comes back: presence now fresh.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE agent_presence SET last_seen=? WHERE asset_id='a'", (time.time(),))
    assert al.detect_offline(_tx(path), _audit) == []  # no new event while healthy
    # It re-emits only after going silent again past the debounce.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE agent_presence SET last_seen=? WHERE asset_id='a'", (time.time() - 200,))
    assert len(al.detect_offline(_tx(path), _audit)) == 1


def test_offline_event_fans_out_to_webhook(env):
    c, h, _, path = env
    # Configure the outbound webhook; the offline event should queue a push.
    c.put("/api/v1/alert-notify/config", headers=h,
          json={"url": "https://hooks.slack.com/services/T/B/X", "channel": "auto", "enabled": True})
    now = time.time()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO agent_presence(asset_id,instance_id,agent_version,protocol_version,last_seen,busy,busy_by,busy_task) "
                   "VALUES('a','i','1','1.1',?,0,NULL,NULL)", (now - 200,))
    al.detect_offline(_tx(path), _audit)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute("SELECT * FROM alert_notifications")]
    assert len(rows) == 1 and rows[0]["dedupe_key"].startswith("offline:")
    assert "失联" in rows[0]["title"]


def test_offline_no_config_is_noop(env):
    _, _, _, path = env
    now = time.time()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO agent_presence(asset_id,instance_id,agent_version,protocol_version,last_seen,busy,busy_by,busy_task) "
                   "VALUES('a','i','1','1.1',?,0,NULL,NULL)", (now - 200,))
    assert len(al.detect_offline(_tx(path), _audit)) == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM alert_notifications").fetchone()[0] == 0


def test_retired_asset_not_reported_offline(env):
    c, h, _, path = env
    _retire(c, h, "a")
    now = time.time()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO agent_presence(asset_id,instance_id,agent_version,protocol_version,last_seen,busy,busy_by,busy_task) "
                   "VALUES('a','i','1','1.1',?,0,NULL,NULL)", (now - 200,))
    assert al.detect_offline(_tx(path), _audit) == []
    assert c.get("/api/v1/assets/offline", headers=h).json() == []


def _tx(path):
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


def _audit(db, event, entity_id, actor, details):
    db.execute("INSERT INTO audit(event,entity_id,actor,details,created_at) VALUES(?,?,?,?,?)",
               (event, entity_id, actor, json.dumps(details, ensure_ascii=False), time.time()))
