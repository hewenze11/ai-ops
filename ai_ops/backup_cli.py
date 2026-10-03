"""Operator CLI for backups: ``ai-ops-backup`` and ``ai-ops-restore``.

These run against the database file directly, so they work both from inside the
service container and from the host that owns the mounted ``state`` directory.
No secret is read: a backup never contains the admin token or SSH keys.
"""
import argparse
import json
import sys
from pathlib import Path

from . import backup


def _default_db():
    import os
    return os.environ.get("AI_OPS_DB", "data/control.db")


def backup_main(argv=None):
    parser = argparse.ArgumentParser(prog="ai-ops-backup", description="Write a consistent snapshot of the control database.")
    parser.add_argument("--db", default=_default_db(), help="control database path")
    parser.add_argument("--out", required=True, help="destination file for the snapshot")
    args = parser.parse_args(argv)
    try:
        meta = backup.create_backup(args.db, args.out)
    except backup.BackupError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps({"ok": True, "backup": args.out, **meta}, ensure_ascii=False))
    return 0


def restore_main(argv=None):
    parser = argparse.ArgumentParser(prog="ai-ops-restore", description="Replace the control database with a verified backup.")
    parser.add_argument("--db", default=_default_db(), help="control database path to overwrite")
    parser.add_argument("--from", dest="source", required=True, help="backup file to restore")
    parser.add_argument("--check", action="store_true", help="verify the backup and exit without restoring")
    args = parser.parse_args(argv)
    try:
        if args.check:
            result = backup.verify_backup(args.source)
            print(json.dumps({"ok": True, "verified": args.source, **result}, ensure_ascii=False))
            return 0
        result = backup.restore_backup(args.source, args.db)
    except backup.BackupError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


def verify_main(argv=None):
    parser = argparse.ArgumentParser(prog="ai-ops-verify-backup", description="Validate a backup file without restoring it.")
    parser.add_argument("backup", help="backup file to validate")
    args = parser.parse_args(argv)
    try:
        result = backup.verify_backup(args.backup)
    except backup.BackupError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps({"ok": True, "backup": args.backup, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(backup_main())
