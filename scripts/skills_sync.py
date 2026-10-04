#!/usr/bin/env python3
"""Sync Skills from the AI Ops website hub into this control service.

This is the operator-facing front door for the same logic the console route
``POST /api/v1/skills/sync`` exposes. It talks to the running control service
over its admin API, so it needs no database access itself.

Typical cron / CI usage:

    # pull once, binding every pulled skill to the local role "ops"
    python scripts/skills_sync.py \\
        --control http://127.0.0.1:8765 \\
        --admin-token-file /etc/ai-ops/admin.token \\
        --hub https://your-site.example/api/v1/skills/repo \\
        --pull-key-file /etc/ai-ops/skills.key \\
        --role ops

Secrets are read from files (preferred) or the environment, never from argv, so
they do not leak into process listings:

    AI_OPS_ADMIN_TOKEN_FILE   admin token for the control service
    AI_OPS_SKILLS_PULL_KEY    subscriber pull key for the hub (aiops-sk-...)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _read_secret(path: str | None, env: str) -> str:
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as e:
            sys.exit(f"cannot read secret file {path}: {e}")
    value = os.environ.get(env, "").strip()
    if not value:
        sys.exit(f"missing secret: pass --{env.lower().replace('_', '-')} or set {env}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Skills from the website hub.")
    parser.add_argument("--control", default=os.environ.get("AI_OPS_CONTROL_URL", "http://127.0.0.1:8765"),
                        help="control service base URL")
    parser.add_argument("--hub", required=True,
                        help="hub endpoint, e.g. https://site/api/v1/skills/repo")
    parser.add_argument("--admin-token-file", default=os.environ.get("AI_OPS_ADMIN_TOKEN_FILE"))
    parser.add_argument("--pull-key-file", default=None,
                        help="file holding the subscriber pull key (aiops-sk-...)")
    parser.add_argument("--role", action="append", default=None,
                        help="bind every pulled skill to this local role (repeatable). "
                             "Omit to keep each skill's own roles, minus unknown ones.")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()

    admin_token = _read_secret(args.admin_token_file, "AI_OPS_ADMIN_TOKEN")
    pull_key = _read_secret(args.pull_key_file, "AI_OPS_SKILLS_PULL_KEY")

    body = {"source_url": args.hub, "pull_key": pull_key, "dry_run": args.dry_run}
    if args.role:
        body["role_ids"] = args.role

    url = args.control.rstrip("/") + "/api/v1/skills/sync"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Authorization": "Bearer " + admin_token,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        sys.exit(f"sync failed: HTTP {e.code} {detail}")
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"sync failed: cannot reach control service: {e}")

    counts = result.get("counts", {})
    print(f"bundle_revision={result.get('bundle_revision')} "
          f"imported={counts.get('imported')} skipped={counts.get('skipped')} "
          f"dry_run={result.get('dry_run')}")
    for item in result.get("imported", []):
        dropped = item.get("dropped_roles")
        suffix = f" (dropped roles: {','.join(dropped)})" if dropped else ""
        print(f"  + {item['id']} -> roles {item.get('role_ids')}{suffix}")
    for item in result.get("skipped", []):
        print(f"  - {item.get('id')}: {item.get('reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
