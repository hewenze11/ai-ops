"""Backup and restore for the control database.

SQLite carries roles, assets, tasks, audit, output archives, leases and turns.
Role-provider credentials are copied to a protected .role-secrets sibling
folder; never embedded in SQLite or API metadata. Managed SSH credentials use
protected .connector-secrets ciphertext and .connector-keys key sidecars; both
must be protected and retained with the snapshot. Legacy SSH file references,
admin and global model credentials remain external and are NOT included.

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
from contextlib import closing
from pathlib import Path

# Highest schema version this build understands. Restoring a backup written by a
# newer build is refused: an older binary cannot know what a newer schema means.
SUPPORTED_SCHEMA = 9
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
    # Flush the snapshot to disk before the sidecar claims it exists. On Windows
    # os.fsync requires a handle with write access, so open read-write and close
    # it explicitly rather than relying on the read-only context manager flush.
    flush = open(tmp, "rb+")
    try:
        os.fsync(flush.fileno())
    finally:
        flush.close()
    report = inspect(tmp)
    refs = _credential_refs(tmp)
    secret_source = Path(os.environ.get('AI_OPS_ROLE_SECRET_DIR') or source.parent / 'role-secrets')
    _copy_role_secrets(refs, secret_source, Path(str(out) + '.role-secrets'))
    from .attachments import backup_files, copy_files, root_for
    attachment_files = backup_files(tmp)
    try:
        copy_files(attachment_files, root_for(source), Path(str(out) + '.attachments'))
    except (ValueError, OSError) as error:
        raise BackupError('Attachment backup incomplete: ' + type(error).__name__) from None
    from .connector_secrets import backup_managed, InvalidToken
    try:
        connector_secret_count = backup_managed(tmp, out)
    except (ValueError, OSError, sqlite3.Error, InvalidToken) as error:
        raise BackupError('Managed SSH credential backup incomplete: ' + type(error).__name__) from None
    os.replace(tmp, out)
    meta = {"created_at": time.time(), "source": str(source), "schema_version": report["schema_version"],
            "size": report["size"], "sha256": _sha256(out), "counts": report["counts"],
            "role_secrets_included": bool(refs), "role_secret_count": len(refs),
            "attachment_file_count": len(attachment_files),
            "connector_secret_count": connector_secret_count,
            "connector_secrets_included": bool(connector_secret_count)}
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
    refs = _credential_refs(out)
    folder = Path(str(out) + '.role-secrets')
    if any(not (folder / (ref + '.key')).is_file() for ref in refs):
        raise BackupError('Role credential backup is missing; keep the .role-secrets sibling folder with this snapshot')
    _enforce_secret_permissions(folder)
    from .attachments import backup_files, verify_files
    try:
        verify_files(backup_files(out), Path(str(out) + '.attachments'))
    except (ValueError, OSError):
        raise BackupError('Keep the complete, unmodified .attachments folder with this snapshot') from None
    from .connector_secrets import verify_managed, InvalidToken
    try:
        verify_managed(out)
        for suffix in ('.connector-secrets', '.connector-keys'):
            _enforce_secret_permissions(Path(str(out) + suffix))
    except (ValueError, OSError, sqlite3.Error, InvalidToken):
        raise BackupError('Managed SSH credential backup is incomplete or invalid; keep both '
                          '.connector-secrets and .connector-keys folders with this snapshot') from None
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
    refs = _credential_refs(backup_path)
    target_secrets = Path(os.environ.get('AI_OPS_ROLE_SECRET_DIR') or live.parent / 'role-secrets')
    _copy_role_secrets(refs, Path(str(backup_path) + '.role-secrets'), target_secrets)
    from .attachments import backup_files, copy_files, root_for
    try:
        copy_files(backup_files(backup_path), Path(str(backup_path) + '.attachments'), root_for(live))
    except (ValueError, OSError):
        raise BackupError('Attachment restore failed; live database was not replaced') from None
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
    from .connector_secrets import restore_managed, InvalidToken
    import uuid
    safety = None
    old_moved = False
    created_secrets = []
    try:
        inspect(staged)
        # Only the staged DB is rewritten. Existing destination credentials and
        # master key are retained, so the safety DB keeps its usable references.
        created_secrets = restore_managed(backup_path, staged, live)
        _checkpoint(staged)
        _leave_wal_mode(staged)
        inspect(staged)
        with open(staged, 'rb+') as handle:
            os.fsync(handle.fileno())
        if live.exists():
            # Restore requires a stopped service; preserve committed WAL data.
            _checkpoint(live)
            _leave_wal_mode(live)
            safety = live.with_name(live.name + '.pre-restore-%d-%s' %
                                    (int(time.time()), uuid.uuid4().hex[:8]))
            for attempt in range(20):
                try:
                    os.replace(live, safety)
                    old_moved = True
                    break
                except PermissionError:
                    if attempt == 19:
                        raise BackupError('Cannot take the live database offline for restore')
                    time.sleep(0.25)
        for suffix in ('-wal', '-shm'):
            Path(str(live) + suffix).unlink(missing_ok=True)
        os.replace(staged, live)
    except BaseException as error:
        try:
            if old_moved:
                os.replace(safety, live)
        except OSError:
            raise BackupError('Restore installation and rollback failed; previous database is '
                              'preserved in the .pre-restore sibling; service must remain stopped') from None
        finally:
            for secret in created_secrets:
                Path(secret).unlink(missing_ok=True)
            for suffix in ('', '-wal', '-shm', '-journal'):
                Path(str(staged) + suffix).unlink(missing_ok=True)
        if isinstance(error, (ValueError, OSError, sqlite3.Error, InvalidToken)):
            raise BackupError('Restore failed; live database was not replaced: ' + type(error).__name__) from None
        raise
    return {"restored_from": str(backup_path), "database": str(live),
            "previous_kept_at": str(safety) if safety else None}


def _credential_refs(path):
    from .role_provider import RoleProviders
    refs = set()
    with closing(sqlite3.connect('file:' + Path(path).as_posix() + '?mode=ro', uri=True)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='role_models'").fetchone():
            for row in db.execute('SELECT config FROM role_models'):
                ref = json.loads(row[0]).get('credential_ref')
                if ref:
                    try:
                        RoleProviders('.').path(ref)
                    except Exception:
                        raise BackupError('Invalid role credential reference') from None
                    refs.add(ref)
    return refs


def _copy_role_secrets(refs, source, destination):
    if not refs:
        return
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    for ref in refs:
        origin, target = source / (ref + '.key'), destination / (ref + '.key')
        if not origin.is_file() or origin.is_symlink():
            raise BackupError('Referenced role credential is unavailable')
        if target.exists():
            if target.is_symlink() or target.read_bytes() != origin.read_bytes():
                raise BackupError('Role credential revision conflict')
            continue
        temporary = target.with_suffix('.partial')
        try:
            with open(temporary, 'xb') as output:
                os.chmod(temporary, 0o600)
                output.write(origin.read_bytes())
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _enforce_secret_permissions(folder):
    """A role-secret folder must not be world/group readable. Best effort on
    POSIX; a no-op on platforms without POSIX modes (Windows test hosts)."""
    if not folder.exists() or os.name == 'nt':
        return
    try:
        os.chmod(folder, 0o700)
        for entry in folder.iterdir():
            if entry.is_file():
                os.chmod(entry, 0o600)
    except OSError:
        raise BackupError('Role credential backup permissions cannot be enforced')


def _leave_wal_mode(path):
    """Switch a stopped database out of WAL. Best effort: some platforms raise
    while a stale reader still holds the file, and the checkpoint already made
    the main file self-contained, so the caller can still proceed."""
    try:
        db = sqlite3.connect(str(path), timeout=30)
        try:
            db.execute("PRAGMA journal_mode=DELETE")
            db.commit()
        finally:
            db.close()
    except sqlite3.Error:
        pass


def _checkpoint(path):
    """Flush a WAL-mode database fully into its main file.

    Opening read-write can fail on Windows while another process holds the file
    (e.g. an open TestClient connection), so fall back to a read-only connection,
    which is enough to run the checkpoint.
    """
    try:
        db = sqlite3.connect(str(path), timeout=30)
    except MemoryError:
        raise
    except sqlite3.Error:
        db = sqlite3.connect("file:%s?mode=ro" % Path(path).as_posix(), uri=True, timeout=30)
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.commit()
    finally:
        db.close()


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
