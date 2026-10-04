# AI Ops 主服务

轻量运维工作台。**当前是 0.1.0.dev5 后端预览，不是已完成产品，也不建议暴露公网。**

## 快速开始（一条命令装主服务）

在一台装了 Docker 的 Linux 机器上（root / sudo）：

```sh
curl -fsSL https://raw.githubusercontent.com/hewenze11/ai-ops/main/deploy/install-control.sh | sudo bash
```

脚本会自己装 Docker（如缺）、生成 `admin_token`、用发布镜像起服务（默认 `ghcr.io/hewenze11/ai-ops:latest`，仅监听 `127.0.0.1:8765`），建好初始角色 `ops` 与资产 `agent-1`，并在最后打印**一条配对码**。

拿到配对码后，在**你要让 AI 操作的机器**上（root / sudo）执行：

```sh
curl -fsSL https://raw.githubusercontent.com/hewenze11/ai-ops/main/deploy/install-agent.sh | sudo bash -s -- --pairing-code 'aiops1-...'
```

Agent 脚本会创建两个最小权限账号（`aiops_read` 只读、`aiops_ops` 可改且仅限服务重启类有限 sudo），把 agent 装进 `/opt/ai-ops-agent` 并注册为 systemd 服务；模型提出的命令在 confirm 模式下要你逐条批准才会执行。

> 前置条件：两个 GHCR 容器包需为 **public** 才能匿名拉取镜像（见下方「CI 与发布」）。若包仍为私有，脚本会在本地已有镜像时回退使用本地副本，否则会明确报错。详细说明见 [deploy/README.md](deploy/README.md)。

## 本次实现

- FastAPI + SQLite 的认证 API，独立资产凭据（数据库仅存其哈希）。
- 角色、资产备注、每轮账号名单快照、逐操作实际账号。
- 事务内的角色 FIFO 队列、确认后入队、未领取任务取消。
- Agent 协议 1.1：领取任务、回传结果、幂等提交与结果重试；1.0 仍兼容。
- Agent 心跳与在线状态查询；在线状态只用于观测，离线不会把已领取任务退回队列。
- 运行中取消：管理员请求 + Agent 确认，任务结果如实保留；未知执行不可用取消解除。
- 原始输出归档：stdout/stderr 分块上传、摘要校验、按字节下载，超限标 `complete=false`。
- 任务租约（观测用，过期不自动重派）与「需要人看」清单：未知执行、过期租约、离线资产。
- 未知执行人工处置：确认成功/确认失败/搁置，带必填说明与独立操作流水，不能靠取消解除。
- 状态变更与审计同事务落盘；未知执行不自动重跑。
- Docker / Compose、GitHub Actions 测试和 GHCR 镜像构建。
- 「定制任务」后端：触发任务与定时任务共用提示词、角色、账号及执行模式配置。
- 五段Linux风格Cron、IANA时区、未来触发时间预览；定时器通过HTTP调用统一trigger接口。
- 持久化定时投递箱、重试去重、配置快照、暂停/删除保留历史。
- 统一角色轮次队列：聊天、触发/定时输入及手工命令共享角色顺序；一轮支持多次模型/工具交互。
- **Web 控制台第一版**：后端内嵌的零依赖静态页（`ai_ops/console/`），只读优先。凭据只存标签页内存、不落浏览器存储；访问 `/` 即可打开。见 [docs/console.md](docs/console.md)。
- **消息渠道第一层**：飞书/企业微信/微信共用同一角色的记忆与串行队列；一次性配对码绑定身份，按身份隔离出站。见 [docs/channels.md](docs/channels.md)。
- **告警结果外推**：Alertmanager 是第一棒（原始告警），AI Ops 是第二棒（AI 排查后的结论）——推的是“结论”不是“原始告警”。一个 Webhook 覆盖飞书/钉钉/企微/Slack/Discord（按 URL 主机自动识别）；持久化 outbox + 有界重试；**SSRF 防护**为安全底线。见 [docs/alert-webhook.md](docs/alert-webhook.md)。
- **管理写入面**：控制台可改名/备注资产/允许账号、编辑文档、创建角色、注册资产、创建/编辑定制任务，以及管理并注入本地 Skills（Skill 是数据不是代码）。一次性凭据只在创建时出示。见 [docs/management.md](docs/management.md)。
- **P3 真机验收已完成**：12 项端到端检查全部通过（含控制台真机截图）。见 [docs/p3-acceptance-report.md](docs/p3-acceptance-report.md)。
- **真机连贯性联调已完成**：用真实付费模型在预览机跑 11 项检查全通过（自我认知/Skill 与文档角色隔离注入/权限注入/confirm 真机执行/同日与跨日记忆回忆/多步连贯/产品自我认知）。模型知识自己属于「AI Ops」产品、产品提供哪些能力面、以及哪些才是它自己的工具。见 [docs/system-self-doc.md](docs/system-self-doc.md) 与 [docs/model-turns.md](docs/model-turns.md)。
- OpenAI兼容模型适配器、角色模型覆盖、独立账号/确认校验、模型调用与回复审计。
- 核心/角色文档编辑API，每次模型调用重新全文注入核心文档与完整服务OpenAPI。
- 可选模型工作线程，角色默认不启用付费调用，必须显式配置。

定制任务API和当前处理边界见 [docs/custom-tasks.md](docs/custom-tasks.md)。调度器默认启用，可用 `AI_OPS_SCHEDULER_ENABLED=0` 禁用调度线程。

## 明确尚未实现

OpenClaw 集成（已定：不自研 fork，见 docs/self-built-route.md）、模型生成的**高质量梗概**（按天分层记忆已实现，见 docs/memory.md）、**内置告警日志已实现（见 docs/alarms.md）**、**Web 控制台第一版已实现（见 docs/console.md）**、**消息渠道第一层已实现（见 docs/channels.md）**、**管理写入面（角色创建/资产注册/文档/Skills/定制任务创建编辑）已实现（见 docs/management.md）**、飞书/微信的**真实出站发送与平台签名校验**（属部署侧）仍需完成。原始输出归档与运行中取消已在协议 1.1 实现，但仅限声明 1.1 的 Agent；`POST /tasks` 仍是管理员单命令入口；模型聊天使用 `/api/v1/roles/{id}/messages`，模型只能通过受控工具创建该轮次的子任务。SSH Connector、输出保留配额、备份恢复、输出/审计的敏感信息脱敏、以及凭据轮换（资产 token 轮换 + admin token 热轮换）已实现（见 `docs/` 下对应文档）。

模型配置与边界见 [docs/model-turns.md](docs/model-turns.md)，执行协议 1.1（心跳、取消、原始输出归档）见 [docs/protocol-v11.md](docs/protocol-v11.md)，Agent 安装与升级见 [docs/operations.md](docs/operations.md)，租约与未知执行处置见 [docs/leases.md](docs/leases.md)。不自动把提示词当命令；模型请求启用非流式响应，遇到不支持的响应或未知执行状态明确停止。

主服务与 Agent 暂以 Python 实现以尽快验证执行契约；**产品主体自研，不 fork OpenClaw**，但借鉴其渠道抽象/上下文引擎/凭据边界/可插拔 provider 的设计（见 [docs/openclaw-assessment.md](docs/openclaw-assessment.md) 与 [docs/self-built-route.md](docs/self-built-route.md)），执行协议不依赖未来模型编排实现。联网搜索的 provider 取舍、SearXNG 默认与合规边界见 [docs/search.md](docs/search.md)。

## 产品定位与商业模式（说明）

产品的定位、设计立场与商业方向见 **[docs/product-overview.md](docs/product-overview.md)**（写给看代码库的人）。
要点：本仓库是产品的**技术底座**，**不自带“内置诊断策略库”**——运维提示词是用户自己的内容；
商业形态为**教学/官网**与**订阅制 Skills 库**两条路径，本仓不包含计费与商城。

面向只想读源码的技术用户：本 README 与 `docs/` 已给出完整实现说明。

## 仓库边界

- 本仓库：主服务、协议权威文档，后续前端与 Connector。
- `hewenze11/ai-ops-agent`：Linux 执行端，独立发布；不需要模型 Key。

## 本地测试

```sh
python -m venv .venv
. .venv/bin/activate
pip install '.[test]'
pytest -q
```

## Linux 单机预览部署

要求 Docker / Compose。以下目录均属本项目，不要使用旧服务的数据目录。

```sh
mkdir -p state secrets
chmod 700 secrets
python3 -c 'import secrets; from pathlib import Path; Path("secrets/admin_token").write_text(secrets.token_urlsafe(48))'
chown -R 10001:10001 state secrets
chmod 600 secrets/admin_token
docker compose -p ai-ops-preview up -d --build
```

默认仅监听宿主机 `127.0.0.1:18765`。远程测试通过 SSH 隧道，或配置有认证和 TLS 的反向代理；不要直接对公网开放管理接口。管理 Token 使用只读文件挂载；不放在命令行参数、Git 或模型上下文。

`GET /healthz` 为健康检查。`/docs` 为临时 API 联调界面，不是产品前端。

使用管理凭据依次调用：`POST /api/v1/roles` → `POST /api/v1/assets`（一次性返回 Agent Token，安全写入目标机配置）→ `POST /api/v1/tasks`。Agent Token 只能领取和回传它自己的资产任务，不能创建任务或读取全局审计。

## 安全与恢复边界

1. 管理端任务入口是受信任的授权边界，未来模型不能直接控制它的本轮授权名单。此版本没有提供模型工具。
2. 同一资产仅有 Agent 接入，本预览未实现 Connector，未来二选一而非双路执行。
3. 只读与可修改由原生账号权限实现，账号名字不提供权限保证。
4. 任务领取与审计在事务内完成后才响应。响应丢失会留在 claimed，不会自动回到队列。
5. Agent 崩溃恢复报告 unknown，阻塞同角色后续任务，防止未经确认继续修改。当前需人工调查，不能通过删数据库解除。
6. 输出按流归档，上限 64 MiB；超限、磁盘错误或流未正常结束时标记 `complete=false`，低层预览字段仍限 64 KiB。归档摘要与结果回传绑定，不能只报摘要。
7. 租约过期不自动重派：自动重发未知是否已执行的命令风险高于等待人工判断。未知执行只能由操作者确认或搁置，不能靠取消解除。
8. 任务结果的内联输出与审计详情在入库前做**尽力而为**的敏感信息脱敏（模式匹配 + 运维声明字面量，见 `docs/scrubbing.md`）；原始输出归档为逐字节证据与 sha256 校验，刻意不改写。这不等同于“任意输出都能可靠清理”，仍不要在任务中传入秘密；不得让低权限用户读取数据库。
9. 主服务不以 root 运行，Compose 丢弃 capabilities、只读文件系统。资产系统用户权限及平台部署安全仍由操作者配置。
10. 取消是“请求 + Agent 确认”，不是服务端单方面终止；Agent 离线或断连时不保证已停止，也不自动重放。未知执行必须人工处置。
11. 普通进程组清理不等于 cgroup 级后代清理；不可宣称支持任意守护进程的可靠停止。

## CI 与发布

push/PR 运行 Python 3.11/3.12 测试。测试通过后构建镜像；非 PR 推送 `ghcr.io/hewenze11/ai-ops`，以提交 SHA (`sha-<commit>`)、默认分支 (`main`) 和 `latest` 标签区分。使用 Actions 内置 GITHUB_TOKEN，不需把个人 PAT 存入工作流。没有自动 SSH 部署，避免每次提交直接改测试机。

> **一次性人工步骤（仅需做一次）**：GitHub 的**用户级容器包可见性无法用 API 修改**（REST 对所有变体都 404，GraphQL 无该字段），必须到网页把包改成 **Public**：
> - github.com/users/hewenze11/packages/container/ai-ops/settings
> - github.com/users/hewenze11/packages/container/ai-ops-agent/settings
>
> 否则陌生人无法匿名 `docker pull`，一键脚本拿不到镜像。

许可证采用 **AGPL-3.0**（见 [LICENSE](LICENSE)）。你可以自由自托管、修改与商用；但若把它作为网络服务提供给他人，需按 AGPL 提供你的修改源码。
