#!/usr/bin/env bash
# AI Ops — one-click EXECUTION AGENT installer (the machine the AI operates).
#
# It answers one question: "make this box ready for the AI, without me reading
# a manual." Everything it needs to reach the control service comes in a single
# pairing code produced by install-control.sh.
#
# What it does:
#   1. creates two native execution accounts with least privilege:
#        aiops_read  — for read-only work (no sudo, no docker, journal+log groups only)
#        aiops_ops   — for change work (sudo only for the units you name, no wildcards)
#   2. installs the agent into its own venv under /opt/ai-ops-agent;
#   3. writes a 0600 config and a hardened systemd unit, and starts it;
#   4. prints the verified status.
#
# Usage (as root, with sudo):
#   curl -fsSL https://raw.githubusercontent.com/hewenze11/ai-ops/main/deploy/install-agent.sh \
#     | sudo bash -s -- --pairing-code 'aiops1-...'
#
# Options:
#   --pairing-code <code>   required; from install-control.sh
#   --read-user  <name>     read-only account name (default aiops_read)
#   --ops-user   <name>     change account name (default aiops_ops)
#   --no-sudo               do NOT grant aiops_ops any sudo (strictest)
#   --ops-units "a.service b.service"   units aiops_ops may restart/reload
#                           (no wildcard; without this the account has no sudo)
#   --journal-dir <path>    agent journal dir (default /var/lib/ai-ops-agent)
#   --ops-units "a.service b"   exact units aiops_ops may restart/reload/start/stop
#                           (no wildcard; without it the account gets no sudo)
set -euo pipefail

PAIRING=""
READ_USER="aiops_read"
OPS_USER="aiops_ops"
GRANT_SUDO=1
OPS_UNITS=""
JOURNAL="/var/lib/ai-ops-agent"
REPO_PIP="git+https://github.com/hewenze11/ai-ops-agent.git"

while [ $# -gt 0 ]; do
  case "$1" in
    --pairing-code) PAIRING="$2"; shift 2;;
    --read-user) READ_USER="$2"; shift 2;;
    --ops-user) OPS_USER="$2"; shift 2;;
    --no-sudo) GRANT_SUDO=0; shift;;
    --ops-units) OPS_UNITS="$2"; shift 2;;
    --ops-units=*) OPS_UNITS="${1#*=}"; shift;;
    --journal-dir) JOURNAL="$2"; shift 2;;
    --allow-root-readonly) ALLOW_ROOT_READONLY=1; shift;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done

say() { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die() { printf '\n\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "run as root (use: ... | sudo bash -s -- --pairing-code '...')"
[ -n "$PAIRING" ] || die "--pairing-code is required (copy it from install-control.sh output)"

# --- prerequisites (python3 is used from here on) ------------------------
if ! command -v python3 >/dev/null 2>&1; then
  say "python3 not found — installing via the system package manager"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq python3 python3-venv || die "could not install python3"
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q python3 || die "could not install python3"
  elif command -v yum >/dev/null 2>&1; then
    yum install -y -q python3 || die "could not install python3"
  else
    die "python3 is required and no known package manager was found"
  fi
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' || die "Python 3.11+ is required"
command -v useradd >/dev/null 2>&1 || die "useradd is required (shadow-utils / passwd package)"

# --- decode the pairing code ---------------------------------------------
PAIRING="$PAIRING" python3 - <<'PY'
import base64, json, os, sys
raw = os.environ["PAIRING"].strip()
if not raw.startswith("aiops1-"):
    sys.exit("pairing code must start with 'aiops1-'")
body = raw[len("aiops1-"):]
body += "=" * (-len(body) % 4)
try:
    blob = json.loads(base64.urlsafe_b64decode(body))
except Exception as exc:
    sys.exit("pairing code is corrupt: %s" % exc)
for key in ("server_url", "asset_id", "agent_token"):
    if not blob.get(key):
        sys.exit("pairing code is missing %r" % key)
with open("/tmp/.aiops-pairing.json", "w") as handle:
    json.dump(blob, handle)
os.chmod("/tmp/.aiops-pairing.json", 0o600)
print("server_url=%s asset_id=%s" % (blob["server_url"], blob["asset_id"]))
PY

SERVER_URL="$(python3 -c 'import json; print(json.load(open("/tmp/.aiops-pairing.json"))["server_url"])')"
ASSET_ID="$(python3 -c 'import json; print(json.load(open("/tmp/.aiops-pairing.json"))["asset_id"])')"
AGENT_TOKEN="$(python3 -c 'import json; print(json.load(open("/tmp/.aiops-pairing.json"))["agent_token"])')"

case "$SERVER_URL" in
  https://*) ALLOW_LOOPBACK=0;;
  http://127.0.0.1*|http://localhost*) warn "loopback HTTP pairing: allowed only for local testing"; ALLOW_LOOPBACK=1;;
  *) die "refusing a non-loopback plain-HTTP control URL; use HTTPS for remote hosts";;
esac

# --- 1. least-privilege accounts ----------------------------------------
say "Creating least-privilege execution accounts"
# The accounts must be able to run a command as their target user (the agent
# drops into them), so they need a real shell. Least privilege comes from
# sudo/file permissions, NOT from nologin — a nologin shell makes the agent's
# command spawn fail with PROCESS_START_FAILED.
ACCOUNT_SHELL="$(command -v bash || command -v sh || echo /bin/sh)"
if ! id "$READ_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell "$ACCOUNT_SHELL" "$READ_USER"
  echo "created $READ_USER (read-only)"
else
  echo "$READ_USER already exists"
fi
if ! id "$OPS_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell "$ACCOUNT_SHELL" "$OPS_USER"
  echo "created $OPS_USER (change)"
else
  echo "$OPS_USER already exists"
fi
# Keep the change account out of the docker group: docker == root here.
if getent group docker >/dev/null 2>&1; then
  gpasswd -d "$OPS_USER" docker >/dev/null 2>&1 || true
fi

if [ "$GRANT_SUDO" = "1" ]; then
  SUDOERS="/etc/sudoers.d/ai-ops-agent"
  say "Granting $OPS_USER exact sudo commands (no wildcards); $READ_USER gets none"
  # Why no `systemctl restart *`: the wildcard also matches
  # `restart foo.service --option`, and more importantly it matches units whose
  # ExecStart is an arbitrary program — i.e. root code execution by a second
  # route. The install grants nothing until an operator names the units this
  # host actually runs, and the file is regenerated on every run (idempotent).
  OPS_UNITS="${OPS_UNITS:-}"
  {
    echo "# Managed by install-agent.sh. Only these exact commands, no wildcards."
    echo "# Read-only account gets no sudo at all: the read channel must not be able to escalate."
    if [ -n "$OPS_UNITS" ]; then
      for unit in $OPS_UNITS; do
        case "$unit" in
          *[!A-Za-z0-9@._-]*|'') warn "ignoring invalid unit name: $unit"; continue;;
        esac
        for verb in restart reload start stop; do
          echo "$OPS_USER ALL=(root) NOPASSWD: /usr/bin/systemctl $verb $unit"
        done
        echo "$OPS_USER ALL=(root) NOPASSWD: /usr/bin/systemctl status $unit"
      done
    else
      echo "# No units configured yet: $OPS_USER has no sudo until you re-run with --ops-units."
    fi
  } >"$SUDOERS"
  chmod 440 "$SUDOERS"
  visudo -cf "$SUDOERS" >/dev/null || { rm -f "$SUDOERS"; die "generated sudoers rule is invalid; removed"; }
  [ -n "$OPS_UNITS" ] || warn "No --ops-units given: the change account cannot restart anything yet."
else
  warn "--no-sudo: $OPS_USER will have no sudo at all"
fi

# --- 1b. read grants for the read-only account ---------------------------
# The read channel runs as $READ_USER (§7), so without grants it can read almost
# nothing an operator cares about -- and the tempting fix is to hand it root,
# which destroys the floor. Grant read access to the paths the AI is actually
# meant to look at, nothing more (journal, logs, /proc, service configs).
say "Granting $READ_USER read access to logs and service configuration"
READ_GROUPS="${READ_GROUPS:-systemd-journal adm}"
for group in $READ_GROUPS; do
  if getent group "$group" >/dev/null 2>&1; then
    usermod -aG "$group" "$READ_USER" || warn "could not add $READ_USER to $group"
  fi
done
READ_PATHS="${READ_PATHS:-/var/log /etc/nginx /etc/systemd/system /etc/ai-ops-agent}"
for path in $READ_PATHS; do
  [ -e "$path" ] || continue
  # Read + traverse only: no write bit changed, and no owner change. `setfacl`
  # is not required; chmod on a group is too broad, so use the ACL if present,
  # otherwise fall back to read-only group membership that already applies.
  if command -v setfacl >/dev/null 2>&1; then
    setfacl -m "u:$READ_USER:rX" -R "$path" 2>/dev/null || warn "could not grant read ACL on $path"
  fi
done

# --- 2. agent venv -------------------------------------------------------
say "Installing the agent into /opt/ai-ops-agent"
if [ -d /opt/ai-ops-agent/venv ]; then
  rm -rf /opt/ai-ops-agent/venv
fi
python3 -m venv /opt/ai-ops-agent/venv
/opt/ai-ops-agent/venv/bin/pip install --quiet --upgrade pip
/opt/ai-ops-agent/venv/bin/pip install --quiet "$REPO_PIP"

# --- 3. config + unit + start -------------------------------------------
say "Writing config and systemd unit"
install -d -m 700 /etc/ai-ops-agent "$JOURNAL"
# Core files are 0600 and owned by the agent's run user. The read channel runs
# as an unprivileged account, so it cannot read the agent token or the
# encrypted credential store even if a prompt injection gets it to try (§43.4).
install -d -m 700 /var/lib/ai-ops-agent-credentials
chown -R root:root "$JOURNAL" /var/lib/ai-ops-agent-credentials
chmod 700 "$JOURNAL" /var/lib/ai-ops-agent-credentials
CFG="/etc/ai-ops-agent/config.json"
SERVER_URL="$SERVER_URL" ASSET_ID="$ASSET_ID" AGENT_TOKEN="$AGENT_TOKEN" \
READ_USER="$READ_USER" OPS_USER="$OPS_USER" JOURNAL="$JOURNAL" ALLOW_LOOPBACK="$ALLOW_LOOPBACK" python3 - <<'PY'
import json, os
cfg = {
    "server_url": os.environ["SERVER_URL"],
    "asset_id": os.environ["ASSET_ID"],
    "agent_token": os.environ["AGENT_TOKEN"],
    "allowed_users": [os.environ["READ_USER"], os.environ["OPS_USER"]],
    "journal_dir": os.environ["JOURNAL"],
}
if os.environ.get("ALLOW_LOOPBACK") == "1":
    cfg["allow_loopback_http"] = True
path = "/etc/ai-ops-agent/config.json"
with open(path, "w") as handle:
    json.dump(cfg, handle, indent=2)
os.chmod(path, 0o600)
PY
rm -f /tmp/.aiops-pairing.json

# Prefer the packaged installer (it owns the hardened unit); fall back if absent.
# The installer uses `shutil.which('ai-ops-agent')`, so the venv bin MUST be on
# PATH or it falls back to the bare interpreter and loses the entry point.
export PATH="/opt/ai-ops-agent/venv/bin:$PATH"
if command -v /opt/ai-ops-agent/venv/bin/ai-ops-agent-install >/dev/null 2>&1; then
  LOOP_FLAG=""
  [ "$ALLOW_LOOPBACK" = "1" ] && LOOP_FLAG="--allow-loopback-http"
  ROOT_FLAG=""
  [ "${ALLOW_ROOT_READONLY:-0}" = "1" ] && ROOT_FLAG="--allow-root-readonly"
  /opt/ai-ops-agent/venv/bin/ai-ops-agent-install \
    --server-url "$SERVER_URL" --asset-id "$ASSET_ID" --token "$AGENT_TOKEN" \
    --user "$READ_USER" --user "$OPS_USER" --journal-dir "$JOURNAL" $LOOP_FLAG $ROOT_FLAG >/dev/null
else
  cat >/etc/systemd/system/ai-ops-agent.service <<EOF
[Unit]
Description=AI Ops execution agent (asset ${ASSET_ID})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=/opt/ai-ops-agent/venv/bin/ai-ops-agent --config ${CFG}
Restart=on-failure
RestartSec=5
# Runs as root to switch into the allowed accounts; it must retain the
# privilege to setuid/setgid/setgroups (=NoNewPrivileges would forbid it).
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
ReadWritePaths=${JOURNAL}
LockPersonality=yes
MemoryDenyWriteExecute=yes

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
fi
systemctl enable --now ai-ops-agent >/dev/null 2>&1 || systemctl restart ai-ops-agent
sleep 3

cat <<EOF

============================================================================
 AI Ops execution agent installed on this machine.

   asset id   : $ASSET_ID
   control    : $SERVER_URL
   accounts   : $READ_USER (read-only, no sudo), $OPS_USER (change, sudo: ${OPS_UNITS:-none configured})
   read root  : ${ALLOW_ROOT_READONLY:-0} (0 = the read channel refuses root)
   config     : $CFG (0600)
   journal    : $JOURNAL
   service    : systemctl status ai-ops-agent

 The AI can now propose commands as $READ_USER / $OPS_USER; in confirm mode you
 approve each one before it runs. Verify from the control service:
   curl -fsS -H "Authorization: Bearer <admin token>" $SERVER_URL/api/v1/agents/$ASSET_ID/status
============================================================================
EOF

systemctl --no-pager --full status ai-ops-agent 2>/dev/null | head -n 12 || true
