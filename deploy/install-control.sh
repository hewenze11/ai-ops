#!/usr/bin/env bash
# AI Ops — one-click MAIN SERVICE installer (the "free tier, try it in 5 minutes"
# path). It is deliberately boring and idempotent: no source build, no config
# files to hand-edit, no secrets in shell history.
#
# What it does:
#   1. makes sure Docker + Compose are present (installs Docker on Debian/Ubuntu
#      or RHEL if missing);
#   2. generates an admin token (never printed to the terminal history as an arg);
#   3. starts the control service from the published image;
#   4. creates a starter role and registers a starter asset over the admin API;
#   5. prints ONE pairing code that you paste into install-agent.sh on the
#      machine you want the AI to operate.
#
# Usage (as root, or with sudo):
#   curl -fsSL https://raw.githubusercontent.com/hewenze11/ai-ops/main/deploy/install-control.sh | sudo bash
#
# Options (pass after `-s --`):
#   --bind 0.0.0.0     listen address (default 127.0.0.1; use 0.0.0.0 to reach it
#                      from another host, and put TLS in front in production)
#   --port 8765        host port (default 8765)
#   --dir /opt/ai-ops  install directory (default /opt/ai-ops)
#   --image <ref>      override the image (default ghcr.io/hewenze11/ai-ops:latest)
#   --no-docker-install   refuse to install Docker; fail instead if missing
set -euo pipefail

BIND="127.0.0.1"
PORT="8765"
DIR="/opt/ai-ops"
IMAGE="${AI_OPS_IMAGE:-ghcr.io/hewenze11/ai-ops:latest}"
ALLOW_DOCKER_INSTALL=1

while [ $# -gt 0 ]; do
  case "$1" in
    --bind) BIND="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --dir) DIR="$2"; shift 2;;
    --image) IMAGE="$2"; shift 2;;
    --no-docker-install) ALLOW_DOCKER_INSTALL=0; shift;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done

say() { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die() { printf '\n\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "run as root (use: ... | sudo bash)"

# --- 0. python3 (used for token generation, pairing code, JSON parsing) ---
if ! command -v python3 >/dev/null 2>&1; then
  say "python3 not found — installing via the system package manager"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq python3 || die "could not install python3"
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q python3 || die "could not install python3"
  elif command -v yum >/dev/null 2>&1; then
    yum install -y -q python3 || die "could not install python3"
  else
    die "python3 is required and no known package manager was found; install python3 and re-run"
  fi
fi

# --- 1. Docker ------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  [ "$ALLOW_DOCKER_INSTALL" = "1" ] || die "docker not found; install it or drop --no-docker-install"
  say "Docker not found — installing via get.docker.com"
  curl -fsSL https://get.docker.com | sh || die "Docker install failed; install Docker manually and re-run with --no-docker-install"
fi
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  die "docker compose plugin not found; install it and re-run"
fi
systemctl enable --now docker >/dev/null 2>&1 || true

# --- 2. Layout + admin token ---------------------------------------------
say "Preparing $DIR"
mkdir -p "$DIR/state" "$DIR/secrets" "$DIR/connector"
chmod 700 "$DIR/secrets"
if [ ! -s "$DIR/secrets/admin_token" ]; then
  # 48 bytes urlsafe; written 0600, never passed as a command argument.
  python3 - >"$DIR/secrets/admin_token" <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
  chmod 600 "$DIR/secrets/admin_token"
  echo "generated a new admin token"
else
  echo "reusing the existing admin token"
fi
# The container runs as uid 10001; let it read the mount.
chown -R 10001:10001 "$DIR/state" "$DIR/secrets" 2>/dev/null || true
chmod 600 "$DIR/secrets/admin_token"

# --- 3. Compose file (self-contained) ------------------------------------
cat >"$DIR/compose.yaml" <<EOF
services:
  control:
    image: ${IMAGE}
    restart: unless-stopped
    ports:
      - "${BIND}:${PORT}:8765"
    environment:
      AI_OPS_ADMIN_TOKEN_FILE: /run/secrets/admin_token
    volumes:
      - ./state:/data
      - ./secrets/admin_token:/run/secrets/admin_token:ro
      - ./connector:/run/connector:ro
    read_only: true
    tmpfs:
      - /tmp:rw,noexec,nosuid,size=32m
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
EOF

say "Pulling and starting the control service"
( cd "$DIR" && { $COMPOSE -p ai-ops pull control || {
    # A private/offline registry must not block a local image that already exists.
    docker image inspect "$IMAGE" >/dev/null 2>&1 || die "cannot pull $IMAGE and it is not present locally"
    warn "could not pull $IMAGE; using the local copy";
  }; } && $COMPOSE -p ai-ops up -d )

BASE="http://127.0.0.1:${PORT}"
say "Waiting for $BASE/healthz"
for _ in $(seq 1 60); do
  if curl -fsS "$BASE/healthz" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
[ "${ok:-}" = "1" ] || die "service did not become healthy; check: cd $DIR && $COMPOSE -p ai-ops logs"

# --- 4. Starter role + asset, then a pairing code ------------------------
TOKEN="$(cat "$DIR/secrets/admin_token")"
api() { # api METHOD PATH [JSON]
  local method="$1" path="$2" data="${3:-}"
  if [ -n "$data" ]; then
    curl -fsS -X "$method" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      -d "$data" "$BASE$path"
  else
    curl -fsS -X "$method" -H "Authorization: Bearer $TOKEN" "$BASE$path"
  fi
}

say "Creating the starter role 'ops' (if absent)"
api POST /api/v1/roles '{"id":"ops","name":"Ops"}' >/dev/null 2>&1 || true

ASSET_ID="agent-1"
say "Registering the starter asset '$ASSET_ID' (if absent)"
ASSET_JSON="$(api POST /api/v1/assets '{"id":"agent-1","name":"My first machine","allowed_users":["aiops_read","aiops_ops"]}' 2>/dev/null || true)"
AGENT_TOKEN="$(printf '%s' "$ASSET_JSON" | python3 -c 'import sys,json;
try: print(json.load(sys.stdin).get("agent_token",""))
except Exception: print("")')"
if [ -z "$AGENT_TOKEN" ]; then
  warn "asset 'agent-1' already exists (or registration failed)."
  echo "  • if it already exists, rotate its token to get a fresh pairing code:"
  echo "      curl -fsS -X POST -H \"Authorization: Bearer \$(cat $DIR/secrets/admin_token)\" $BASE/api/v1/assets/agent-1/rotate-token"
  echo "  • or register a different asset id with: $0 --asset <id> (re-run)"
  PAIRING=""
else
  # A pairing code is a single opaque token that bundles everything the agent
  # needs to reach this control service. Safe to copy; treat like a password.
  HOST_FOR_AGENT="$BIND"
  [ "$BIND" = "127.0.0.1" ] && HOST_FOR_AGENT="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$HOST_FOR_AGENT" ] || HOST_FOR_AGENT="127.0.0.1"
  PAIRING="$(PAIRING_SERVER="http://${HOST_FOR_AGENT}:${PORT}" PAIRING_ASSET="agent-1" PAIRING_TOKEN="$AGENT_TOKEN" python3 - <<'PY'
import base64, json, os
blob = {"server_url": os.environ["PAIRING_SERVER"], "asset_id": os.environ["PAIRING_ASSET"],
        "agent_token": os.environ["PAIRING_TOKEN"]}
print("aiops1-" + base64.urlsafe_b64encode(json.dumps(blob).encode()).decode().rstrip("="))
PY
)"
fi

cat <<EOF

============================================================================
 AI Ops control service is up.

   Console : $BASE
   API docs: $BASE/docs   (temporary; not the product UI)
   Admin token (keep secret): $DIR/secrets/admin_token

 NEXT — install the execution agent on the machine you want the AI to operate,
 as root there, and paste this pairing code:

   curl -fsSL https://raw.githubusercontent.com/hewenze11/ai-ops/main/deploy/install-agent.sh | sudo bash -s -- --pairing-code '$PAIRING'

 (If the agent is on another host, make sure this host's $PORT is reachable —
  re-run with --bind 0.0.0.0 and put TLS in front for anything non-local.)
============================================================================
EOF
