"""Backup and restore for the control database.

Everything durable in this service lives in one SQLite database (the same file
carries roles, assets with hashed tokens, tasks, audit, output archives, leases
and turns). Secrets are mounted read-only and are deliberately *not* part of a
backup: a snapshot must never embed an admin token or an SSH private key.

Backups use SQLite's online backup API, which produces a transactionally
consistent snapshot even while the service is running under WAL, without
stopping the writer. A backup is only considered usable after it passes
``PRAGMA integrity_check`` and a schema-version check; restore refuses to touch
the live database unless the candidate passes the same checks first.
"""
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

# Highest schema version this build understands. Restoring a backup written by a
# newer build is refused: an older binary cannot know what a newer schema means.
SUPPORTED_SCHEMA = 6
# A backup is a plain SQLite file plus a sibling ".meta.json" sidecar.
META_SUFFIX = ".meta.json"


class BackupError(Exception):
    """A backup is missing, unreadable, corrupt or incompatible."""


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _schema_version(db):
    return db.execute("PRAGMA user_version").fetchone()[0]


def inspect(path):
    """Open a candidate database read-only and report its health.

    Opens with ``mode=ro`` so inspection can never mutate the file. Raises
    BackupError when the file is missing, not a database, corrupt, or written by
    a schema this build cannot understand.
    """
    candidate = Path(path)
    if not candidate.exists():
        raise BackupError("Backup file does not exist")
    try:
        db = sqlite3.connect("file:%s?mode=ro" % candidate.as_posix(), uri=True, timeout=15)
    except sqlite3.Error as error:
        raise BackupError("Cannot open backup: %s" % type(error).__name__)
    try:
        db.row_factory = sqlite3.Row
        try:
            check = db.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as error:
            raise BackupError("Not a readable SQLite database: %s" % type(error).__name__)
        if check != "ok":
            raise BackupError("Integrity check failed: %s" % check)
        version = _schema_version(db)
        if version < 1:
            raise BackupError("Backup has no control schema (user_version=%d)" % version)
        if version > SUPPORTED_SCHEMA:
            raise BackupError("Backup schema %d is newer than this build supports (%d)" % (version, SUPPORTED_SCHEMA))
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {}
        for name in ("roles", "assets", "tasks", "audit"):
            if name in tables:
                counts[name] = db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
        return {"schema_version": version, "tables": tables, "counts": counts,
                "size": candidate.stat().st_size}
    finally:
        db.close()


def create_backup(db_path, out_path):
    """Write a consistent snapshot of ``db_path`` to ``out_path``.

    Uses the online backup API so a running service is not stopped and the
    snapshot never sees a half-written transaction. Writes a ``.meta.json``
    sidecar with the schema version, digests and counts. The output file is
    created 0600 and fsynced before the sidecar is written, so a crashed backup
    leaves no metadata that claims a complete snapshot it does not have.
    """
    source = Path(db_path)
    if not source.exists():
        raise BackupError("Source database does not exist")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".partial")
    if tmp.exists():
        tmp.unlink()
    # Copy through a temporary file, flush it to disk, then rename atomically.
    src = sqlite3.connect("file:%s?mode=ro" % source.as_posix(), uri=True, timeout=30)
    try:
        dest = sqlite3.connect(str(tmp), timeout=30)
        try:
            src.backup(dest)
            dest.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dest.commit()
        finally:
            dest.close()
    finally:
        src.close()
    os.chmod(tmp, 0o600)
    with open(tmp, "rb") as handle:
        os.fsync(handle.fileno())
    report = inspect(tmp)
    os.replace(tmp, out)
    meta = {"created_at": time.time(), "source": str(source), "schema_version": report["schema_version"],
            "size": report["size"], "sha256": _sha256(out), "counts": report["counts"]}
    sidecar = Path(str(out) + META_SUFFIX)
    with open(sidecar, "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(sidecar, 0o600)
    return meta


def verify_backup(path):
    """Validate a backup file and its sidecar; return the metadata."""
    out = Path(path)
    report = inspect(out)
    sidecar = Path(str(out) + META_SUFFIX)
    meta = None
    if sidecar.exists():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            raise BackupError("Backup metadata is unreadable: %s" % type(error).__name__)
        if meta.get("sha256") and meta["sha256"] != _sha256(out):
            raise BackupError("Backup digest does not match its metadata")
        if meta.get("schema_version") != report["schema_version"]:
            raise BackupError("Backup metadata schema version disagrees with the file")
    return {"report": report, "meta": meta}


def restore_backup(backup_path, db_path):
    """Replace the live database with a verified backup.

    Order matters: the candidate is fully validated *before* the live database
    is touched, and the current database is preserved as a ``.pre-restore``
    sibling so an operator can go back. The live DB is swapped in with an atomic
    rename. WAL/SHM side files of the old database are removed so a stale WAL
    cannot be replayed over the restored file.
    """
    verify_backup(backup_path)
    live = Path(db_path)
    live.parent.mkdir(parents=True, exist_ok=True)
    # Stage the validated copy next to the live DB so the final rename is atomic.
    staged = live.with_name(live.name + ".restore-incoming")
    if staged.exists():
        staged.unlink()
    with open(backup_path, "rb") as src, open(staged, "wb") as dest:
        while True:
            block = src.read(1024 * 1024)
            if not block:
                break
            dest.write(block)
        dest.flush()
        os.fsync(dest.fileno())
    os.chmod(staged, 0o600)
    # Re-verify the staged copy: what we are about to install must be the same
    # healthy snapshot we validated.
    inspect(staged)

    safety = None
    if live.exists():
        safety = live.with_name(live.name + ".pre-restore-%d" % int(time.time()))
        os.replace(live, safety)
    # Remove old WAL/SHM so the restored file is not shadowed by stale journal.
    for suffix in ("-wal", "-shm"):
        stale = Path(str(live) + suffix)
        if stale.exists():
            stale.unlink()
    os.replace(staged, live)
    return {"restored_from": str(backup_path), "database": str(live),
            "previous_kept_at": str(safety) if safety else None}


def install_backup(app, transaction, audit, admin, db_path):
    """Expose on-server snapshot creation. Restore is CLI-only by design.

    An HTTP restore would let a single authenticated call overwrite live state
    with no way to confirm the target path; restoring is deliberately an
    operator action on the host, not an API call.
    """
    import time as _time
    from fastapi import Depends

    backups_dir = Path(db_path).parent / "backups"

    @app.post("/api/v1/backup", dependencies=[Depends(admin)])
    def create_snapshot():
        backups_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = backups_dir / ("control-%d.db" % int(_time.time()))
        try:
            meta = create_backup(db_path, target)
        except BackupError as error:
            from fastapi import HTTPException
            raise HTTPException(409, str(error))
        with transaction() as db:
            audit(db, "backup.created", None, "admin",
                  {"path": str(target), "size": meta["size"], "sha256": meta["sha256"],
                   "schema_version": meta["schema_version"]})
        return {"path": str(target), **meta}

    @app.get("/api/v1/backup", dependencies=[Depends(admin)])
    def list_snapshots():
        if not backups_dir.exists():
            return {"dir": str(backups_dir), "backups": []}
        items = []
        for path in sorted(backups_dir.glob("*.db")):
            entry = {"path": str(path), "size": path.stat().st_size}
            sidecar = Path(str(path) + META_SUFFIX)
            if sidecar.exists():
                try:
                    entry.update(json.loads(sidecar.read_text(encoding="utf-8")))
                except (ValueError, OSError):
                    entry["metadata"] = "unreadable"
            items.append(entry)
        return {"dir": str(backups_dir), "backups": items}
