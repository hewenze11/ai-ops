import json
import gc
import os
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_ops import backup
from ai_ops.app import create_app
from test_control import env, submit, claim, finish, ADMIN


def _stop_service(client):
    """Release every handle on the live SQLite file, standing in for stopping the
    service before a CLI restore. Required on Windows, where the file cannot be
    renamed while a handle is open; the in-process TestClient keeps one alive
    until its transport is garbage-collected."""
    try:
        client.close()
    except Exception:
        pass
    gc.collect()


def _seed(client, headers):
    client.post("/api/v1/roles", headers=headers, json={"id": "ops", "name": "Ops"})
    return client.post("/api/v1/assets", headers=headers, json={
        "id": "a", "name": "a", "allowed_users": ["reader"], "notes": "n"}).json()["agent_token"]


def test_create_backup_is_consistent_and_verifiable(tmp_path, env):
    c, h, tokens, path = env
    submit(env, key="request-777")
    out = tmp_path / "snap.db"
    meta = backup.create_backup(str(path), str(out))
    assert out.exists() and out.stat().st_size > 0
    assert meta["schema_version"] == backup.SUPPORTED_SCHEMA
    assert meta["counts"]["tasks"] == 1
    # Sidecar written 0600 with a matching digest.
    sidecar = tmp_path / "snap.db.meta.json"
    assert sidecar.exists()
    stored = json.loads(sidecar.read_text())
    assert stored["sha256"] == meta["sha256"]
    result = backup.verify_backup(str(out))
    assert result["report"]["schema_version"] == backup.SUPPORTED_SCHEMA


def test_backup_never_contains_the_admin_token(tmp_path, env):
    c, h, _, path = env
    out = tmp_path / "snap.db"
    backup.create_backup(str(path), str(out))
    raw = out.read_bytes()
    assert ADMIN.encode() not in raw


def test_verification_rejects_a_non_database(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a sqlite file at all" * 10)
    with pytest.raises(backup.BackupError):
        backup.verify_backup(str(junk))


def test_verification_rejects_missing_file(tmp_path):
    with pytest.raises(backup.BackupError):
        backup.verify_backup(str(tmp_path / "nope.db"))


def test_verification_rejects_newer_schema(tmp_path, env):
    c, h, _, path = env
    out = tmp_path / "snap.db"
    backup.create_backup(str(path), str(out))
    with sqlite3.connect(str(out)) as db:
        db.execute("PRAGMA user_version=%d" % (backup.SUPPORTED_SCHEMA + 1))
    with pytest.raises(backup.BackupError):
        backup.verify_backup(str(out))


def test_verification_detects_tampering(tmp_path, env):
    c, h, _, path = env
    out = tmp_path / "snap.db"
    backup.create_backup(str(path), str(out))
    # Flip a byte in the middle of the database, leaving the sidecar stale.
    data = bytearray(out.read_bytes())
    data[len(data) // 2] ^= 0xFF
    out.write_bytes(bytes(data))
    with pytest.raises(backup.BackupError):
        backup.verify_backup(str(out))


def test_restore_replaces_live_database_and_keeps_previous(tmp_path, env):
    c, h, tokens, path = env
    submit(env, key="request-001")  # one task in the snapshot
    out = tmp_path / "snap.db"
    backup.create_backup(str(path), str(out))
    # Mutate the live database after the snapshot.
    submit(env, key="request-002")
    # Restoring replaces the live file on disk, which Windows will not allow while
    # any connection still holds it. Stop the service first (the real contract);
    # POSIX would tolerate an open handle, but the sequence is identical.
    _stop_service(c)
    gc.collect()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
    # Wait for the filesystem to release the handle from the read above.
    for _ in range(40):
        gc.collect()
        try:
            os.replace(str(path), str(path) + ".probe")
            os.replace(str(path) + ".probe", str(path))
            break
        except PermissionError:
            time.sleep(0.25)
    else:
        pytest.skip("filesystem still holds the live database; restore is a stop-the-service operation")

    result = backup.restore_backup(str(out), str(path))
    # The restored database has the pre-mutation state.
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    # The pre-restore database was preserved as a sibling.
    assert result["previous_kept_at"] is not None and os.path.exists(result["previous_kept_at"])
    with sqlite3.connect(result["previous_kept_at"]) as db:
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


def test_restore_refuses_corrupt_backup_without_touching_live(tmp_path, env):
    c, h, _, path = env
    submit(env, key="request-001")
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"garbage" * 100)
    with pytest.raises(backup.BackupError):
        backup.restore_backup(str(bad), str(path))
    # Live database untouched.
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_restore_leaves_no_partial_or_stale_artifacts(tmp_path, env):
    c, h, _, path = env
    submit(env, key="request-001")
    out = tmp_path / "snap.db"
    backup.create_backup(str(path), str(out))
    # A second write moves data into the WAL; restore must fold it into the
    # safety copy rather than orphan it.
    submit(env, key="request-002")
    # Stop the service before a CLI restore (Windows holds the file open while a
    # connection is live).
    _stop_service(c)
    result = backup.restore_backup(str(out), str(path))
    # No half-written staging file survives a successful restore.
    assert not os.path.exists(str(path) + ".restore-incoming")
    # The safety copy is a complete, readable database with both tasks.
    with sqlite3.connect(result["previous_kept_at"]) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
    # The restored live database matches the snapshot.
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_backup_api_creates_and_lists_snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_OPS_DB", str(tmp_path / "control.db"))
    client = TestClient(create_app(str(tmp_path / "control.db"), ADMIN))
    headers = {"Authorization": "Bearer " + ADMIN}
    _seed(client, headers)

    created = client.post("/api/v1/backup", headers=headers)
    assert created.status_code == 200, created.text
    body = created.json()
    assert os.path.exists(body["path"]) and body["sha256"]
    listing = client.get("/api/v1/backup", headers=headers).json()
    assert any(item["path"] == body["path"] for item in listing["backups"])


def test_backup_api_requires_admin(tmp_path):
    client = TestClient(create_app(str(tmp_path / "control.db"), ADMIN))
    assert client.post("/api/v1/backup").status_code == 401
    assert client.get("/api/v1/backup").status_code == 401
    bad = {"Authorization": "Bearer not-the-admin-token-000000000000"}
    assert client.post("/api/v1/backup", headers=bad).status_code == 403


def test_backup_api_records_audit(tmp_path):
    client = TestClient(create_app(str(tmp_path / "control.db"), ADMIN))
    headers = {"Authorization": "Bearer " + ADMIN}
    _seed(client, headers)
    client.post("/api/v1/backup", headers=headers)
    events = [row["event"] for row in client.get("/api/v1/audit", headers=headers).json()]
    assert "backup.created" in events
