"""Operator CLI for rotating the administrative credential.

The admin token lives in a read-only secret file, never in the database. The
running service re-reads that file on every check, so replacing it rotates the
credential without a restart. This command writes the new token atomically and
keeps the previous file as a timestamped backup so an operator can roll back.
"""
import argparse
import os
import secrets
import sys
from pathlib import Path

MIN_LENGTH = 32


def rotate(path, length=48):
    """Atomically replace the admin token file, keeping the old one as backup."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    new_token = secrets.token_urlsafe(length)
    if len(new_token) < MIN_LENGTH:
        raise ValueError("Generated token is shorter than the minimum length")
    backup = None
    if target.exists():
        backup = target.with_name(target.name + ".previous")
        # Keep a single rollback copy: overwrite any older one.
        os.replace(target, backup)
    tmp = target.with_name(target.name + ".new")
    # Write with 0600 from the start so the secret is never briefly world-readable.
    handle = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(handle, new_token.encode())
        os.fsync(handle)
    finally:
        os.close(handle)
    os.replace(tmp, target)
    os.chmod(target, 0o600)
    return {"path": str(target), "backup": str(backup) if backup else None, "length": len(new_token)}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ai-ops-admin-token",
                                     description="Rotate the administrative token file in place.")
    parser.add_argument("--path", default=os.environ.get("AI_OPS_ADMIN_TOKEN_FILE", ""),
                        help="admin token file to rotate (defaults to AI_OPS_ADMIN_TOKEN_FILE)")
    parser.add_argument("--length", type=int, default=48, help="token length in bytes of entropy (default 48)")
    args = parser.parse_args(argv)
    if not args.path:
        print("Refusing to run: no --path and AI_OPS_ADMIN_TOKEN_FILE is unset")
        return 2
    try:
        result = rotate(args.path, args.length)
    except (OSError, ValueError) as error:
        print("Rotation failed: %s" % type(error).__name__)
        return 1
    # The token value itself is never printed: only where it was written.
    print("Rotated admin token at %s (previous kept at %s)" % (result["path"], result["backup"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
