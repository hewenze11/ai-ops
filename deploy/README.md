# 一键部署脚本

两条命令把这个预览跑起来：先装**主服务**，再在目标机装**执行 Agent**。

> 当前是 `0.1.0.dev5` 后端预览。默认只监听环回地址，不要直接暴露公网；对外请加带 TLS 的反向代理和认证。

## 1. 装主服务（在跑控制服务的机器上）

```sh
curl -fsSL https://raw.githubusercontent.com/hewenze11/ai-ops/main/deploy/install-control.sh | sudo bash
```

脚本做的事（幂等，重复跑安全）：

1. 缺 Docker 就装（Debian/Ubuntu/RHEL；可用 `--no-docker-install` 禁止）；
2. 生成 `admin_token`，写进 `secrets/admin_token`（0600，不经过命令行参数）；
3. 用发布镜像起服务（默认 `ghcr.io/hewenze11/ai-ops:latest`，监听 `127.0.0.1:8765`）；
4. 建初始角色 `ops` 与资产 `agent-1`；
5. 打印**一条配对码**（`aiops1-...`）。

常用选项（接在 `-s --` 之后）：

| 选项 | 说明 | 默认 |
| --- | --- | --- |
| `--bind <ip>` | 监听地址 | `127.0.0.1` |
| `--port <n>` | 宿主端口 | `8765` |
| `--dir <path>` | 安装目录 | `/opt/ai-ops` |
| `--image <ref>` | 覆盖镜像（或环境变量 `AI_OPS_IMAGE`） | `ghcr.io/hewenze11/ai-ops:latest` |
| `--no-docker-install` | 缺 Docker 时直接失败，不自动安装 | 关 |

想从别的机器访问：`--bind 0.0.0.0`，并在前面加 TLS 反向代理。

## 2. 装执行 Agent（在要被 AI 操作的机器上）

拿到上一步的配对码，在目标机 root/sudo 执行：

```sh
curl -fsSL https://raw.githubusercontent.com/hewenze11/ai-ops/main/deploy/install-agent.sh | sudo bash -s -- --pairing-code 'aiops1-...'
```

配对码本身就是一条不透明 token，打包了 agent 需要的全部信息（控制服务地址、资产 id、agent token）。**当作密码保管。**

脚本会：

1. 解析配对码，得到控制服务地址（环回 HTTP 仅本地测试允许，会有警告）；
2. 创建两个最小权限账号：`aiops_read`（只读）、`aiops_ops`（可改，附**仅限服务重启类**的有限 sudo）；`aiops_read` 不给 sudo；
3. 把 agent 装进 `/opt/ai-ops-agent`（独立 venv），写 0600 配置与 systemd 服务，启动。

常用选项：

| 选项 | 说明 |
| --- | --- |
| `--pairing-code <code>` | 必需，来自主服务安装输出 |
| `--read-user <name>` | 只读账号名（默认 `aiops_read`） |
| `--ops-user <name>` | 可改账号名（默认 `aiops_ops`） |
| `--journal-dir <path>` | 日志目录（默认 `/var/lib/ai-ops-agent`） |
| `--no-sudo` | 不给 `aiops_ops` 任何 sudo |

> 环回 HTTP 配对（`http://127.0.0.1` / `localhost`）脚本会自动放行并警告；非环回的明文 HTTP 会被**直接拒绝**，远程主机请用 HTTPS。

## 3. 后续

- 健康检查：`curl -fsS http://127.0.0.1:8765/healthz`
- 看服务状态：`cd /opt/ai-ops && docker compose -p ai-ops logs -f`
- Agent 状态：`systemctl status ai-ops-agent`

## 前置条件与已知限制

- **GHCR 包必须为 public** 才能匿名拉取镜像。GitHub 的用户级容器包**可见性无法用 API 改**，需到网页设置：`github.com/users/hewenze11/packages/container/ai-ops/settings`（`ai-ops-agent` 同理）。包仍私有且本地无镜像时，主服务脚本会明确报错退出。
- Agent 端 `pip install git+https://github.com/hewenze11/ai-ops-agent.git` 需要该仓库可读（现已 public）。
- 这是预览版：模型提出的命令需逐条人工确认；未知执行必须人工处置，不会自动重跑。
