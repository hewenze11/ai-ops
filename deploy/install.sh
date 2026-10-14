#!/usr/bin/env bash
# AI Ops 主服务 · 一条命令部署脚本
#
# 用法（在目标 Linux 上，exact one-liner）：
#   curl -fsSL https://github.com/hewenze11/ai-ops/releases/latest/download/install.sh | bash
# 或者下载后本地执行：
#   bash install.sh
#
# 这个脚本会：
#   1. 检查必需环境（docker / compose / curl / openssl）
#   2. 缺少的环境，交互式询问你是否安装（不会静默改你的系统）
#   3. 拉取纯净镜像，创建专属目录，生成配置文件与管理员 Token
#   4. 启动服务，并打印访问地址
#
# 不做的事：不安装 Nginx、不配置 TLS、不帮你打开公网、不写入任何模型 Key。

set -euo pipefail

APP_NAME="ai-ops"
IMAGE_DEFAULT="ghcr.io/hewenze11/ai-ops:latest"
INSTALL_DIR="${AI_OPS_DIR:-/opt/ai-ops}"
PORT_DEFAULT="8765"
ASSUME_YES=0

for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    -d=*|--dir=*) INSTALL_DIR="${arg#*=}" ;;
    --dir) shift ;;
    -h|--help)
      sed -n '2,/^set -euo/p' "$0" | sed -e 's/^# \{0,1\}//' -e '/^set -euo/d'
      exit 0 ;;
  esac
done

c_reset=$'\033[0m'; c_ok=$'\033[32m'; c_warn=$'\033[33m'; c_err=$'\033[31m'; c_info=$'\033[36m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$c_ok" "$c_reset" "$*"; }
warn() { printf '%s!%s %s\n' "$c_warn" "$c_reset" "$*"; }
err()  { printf '%s✗%s %s\n' "$c_err" "$c_reset" "$*" >&2; }
step() { printf '\n%s==> %s%s\n' "$c_info" "$*" "$c_reset"; }

ask_yes_no() {
  # $1 = question. Returns 0 for yes.
  if [ "$ASSUME_YES" -eq 1 ]; then return 0; fi
  if [ ! -t 0 ]; then
    warn "非交互环境，默认不安装：$1"
    return 1
  fi
  local reply
  read -r -p "$1 [y/N] " reply </dev/tty || true
  case "$reply" in [yY]|[yY][eE][sS]) return 0 ;; *) return 1 ;; esac
}

require_root_or_sudo() {
  if [ "$(id -u)" -eq 0 ]; then SUDO=""; return; fi
  if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; return; fi
  err "需要 root 权限创建 $INSTALL_DIR，且系统没有 sudo。请用 root 运行。"
  exit 1
}

install_docker() {
  warn "自动安装 Docker 会修改系统软件源并需要 root。"
  if ! ask_yes_no "是否现在自动安装 Docker（使用官方 get.docker.com 脚本）？"; then
    err "请先手动安装 Docker Engine + Compose v2，然后重新运行本脚本。"
    exit 1
  fi
  require_root_or_sudo
  curl -fsSL https://get.docker.com | $SUDO sh
  $SUDO systemctl enable --now docker || true
}

step "检查运行环境"
missing=()
for bin in docker curl; do
  command -v "$bin" >/dev/null 2>&1 && ok "$bin 已安装" || { warn "缺少 $bin"; missing+=("$bin"); }
done
if command -v docker >/dev/null 2>&1; then
  if docker compose version >/dev/null 2>&1; then ok "docker compose v2 已安装"; else warn "缺少 docker compose v2"; missing+=("docker compose"); fi
fi
if command -v openssl >/dev/null 2>&1; then ok "openssl 已安装"; else warn "缺少 openssl（用于生成管理员 Token）"; missing+=("openssl"); fi

if printf '%s\n' "${missing[@]:-}" | grep -q docker; then
  install_docker
fi
if printf '%s\n' "${missing[@]:-}" | grep -q openssl; then
  if ask_yes_no "是否安装 openssl？"; then
    require_root_or_sudo
    if command -v apt-get >/dev/null 2>&1; then $SUDO apt-get update && $SUDO apt-get install -y openssl
    elif command -v dnf >/dev/null 2>&1; then $SUDO dnf install -y openssl
    elif command -v yum >/dev/null 2>&1; then $SUDO yum install -y openssl
    else err "无法自动安装 openssl，请手动安装。"; exit 1; fi
  else
    err "缺少 openssl，无法生成管理员 Token。"; exit 1
  fi
fi

require_root_or_sudo

step "准备专属目录 $INSTALL_DIR"
if [ -e "$INSTALL_DIR" ]; then
  warn "目录已存在：$INSTALL_DIR"
  else
  $SUDO install -d -m 700 -o 10001 -g 10001 "$INSTALL_DIR"
  $SUDO install -d -m 700 -o 10001 -g 10001 "$INSTALL_DIR/state"
  $SUDO install -d -m 700 -o 10001 -g 10001 "$INSTALL_DIR/secrets"
  $SUDO install -d -m 700 -o 10001 -g 10001 "$INSTALL_DIR/logs"
  $SUDO install -d -m 700 -o 10001 -g 10001 "$INSTALL_DIR/memory"
  $SUDO install -d -m 700 -o 10001 -g 10001 "$INSTALL_DIR/connector"
  ok "已创建 state / secrets / logs / memory"
fi

cfg="$INSTALL_DIR/ai-ops.env"
if [ -f "$cfg" ]; then
  ok "配置文件已存在，保留不覆盖：$cfg"
else
  $SUDO tee "$cfg" >/dev/null <<EOF
# AI Ops 主服务配置文件（纯文本，自己编辑）
# 这里只有关键项；改完执行：docker compose --env-file ai-ops.env up -d

# 镜像地址（默认最新发布版）
AI_OPS_IMAGE=${IMAGE_DEFAULT}
# 监听地址：127.0.0.1 只能本机访问；0.0.0.0 允许其它机器访问
AI_OPS_BIND=0.0.0.0
# 控制台端口
AI_OPS_PORT=${PORT_DEFAULT}
# 全局日志目录（容器内映射）
AI_OPS_LOG_DIR=${INSTALL_DIR}/logs
# 角色记忆目录
AI_OPS_MEMORY_DIR=${INSTALL_DIR}/memory
# 可选后台能力，按需改成 1
AI_OPS_LOCAL_CONNECTOR_ENABLED=0
AI_OPS_ALERT_NOTIFY_ENABLED=0
AI_OPS_MEMORY_ARCHIVER_ENABLED=0

# ===== 模型配置（也可以稍后在控制台里填）=====
# 例：OpenAI 兼容接口
# OPENAI_BASE_URL=https://api.openai.com/v1
# OPENAI_MODEL=gpt-4o-mini
# OPENAI_API_KEY=
EOF
  ok "已生成配置文件：$cfg（含示例与说明）"
fi

if [ -f "$INSTALL_DIR/secrets/admin_token" ]; then
  ok "管理员 Token 已存在，保留不覆盖"
else
  $SUDO sh -c "umask 077; openssl rand -hex 32 > '$INSTALL_DIR/secrets/admin_token'"
  $SUDO chown 10001:10001 "$INSTALL_DIR/secrets/admin_token"
  $SUDO chmod 600 "$INSTALL_DIR/secrets/admin_token"
  ok "已生成管理员 Token"
fi

step "写入 Compose 文件"
$SUDO tee "$INSTALL_DIR/docker-compose.yml" >/dev/null <<'EOF'
services:
  control:
    image: ${AI_OPS_IMAGE:-ghcr.io/hewenze11/ai-ops:latest}
    restart: unless-stopped
    ports:
      - "${AI_OPS_BIND:-0.0.0.0}:${AI_OPS_PORT:-8765}:8765"
    environment:
      AI_OPS_ADMIN_TOKEN_FILE: /run/secrets/admin_token
      AI_OPS_LOCAL_CONNECTOR_ENABLED: "${AI_OPS_LOCAL_CONNECTOR_ENABLED:-0}"
      AI_OPS_ALERT_NOTIFY_ENABLED: "${AI_OPS_ALERT_NOTIFY_ENABLED:-0}"
      AI_OPS_MEMORY_ARCHIVER_ENABLED: "${AI_OPS_MEMORY_ARCHIVER_ENABLED:-0}"
    volumes:
      - ./state:/data
      - ./secrets/admin_token:/run/secrets/admin_token:ro
      - ./connector:/run/connector:ro
      - ./logs:/var/log/ai-ops
      - ./memory:/var/lib/ai-ops/memory
    read_only: true
    tmpfs:
      - /tmp:rw,noexec,nosuid,size=32m
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }
EOF
ok "已写入 docker-compose.yml"

step "拉取镜像并启动"
( cd "$INSTALL_DIR" && $SUDO docker compose --env-file ai-ops.env pull && $SUDO docker compose --env-file ai-ops.env up -d --no-build )
ok "容器已启动"

step "等待服务就绪"
ready=0
for _ in $(seq 1 15); do
  if curl -fsS "http://127.0.0.1:${PORT_DEFAULT}/healthz" >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done
[ "$ready" -eq 1 ] && ok "健康检查通过" || warn "健康检查暂未通过，稍等片刻再访问（查看日志排查）"

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
say ""
ok "部署完成。"
say "  控制台地址：http://${IP:-<服务器IP>}:${PORT_DEFAULT}"
say "  登录 Token ：${INSTALL_DIR}/secrets/admin_token  （查看：sudo cat ${INSTALL_DIR}/secrets/admin_token）"
say "  配置文件   ：${INSTALL_DIR}/ai-ops.env"
say "  日志目录   ：${INSTALL_DIR}/logs   记忆目录：${INSTALL_DIR}/memory"
say ""
say "  查看日志：cd ${INSTALL_DIR} && sudo docker compose logs -f control"
say "  停止服务：cd ${INSTALL_DIR} && sudo docker compose down"
