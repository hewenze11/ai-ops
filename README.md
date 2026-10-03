# AI Ops 主服务

轻量运维工作台。**当前是 0.1.0.dev5 后端预览，不是已完成产品，也不建议暴露公网。**

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
- OpenAI兼容模型适配器、角色模型覆盖、独立账号/确认校验、模型调用与回复审计。
- 核心/角色文档编辑API，每次模型调用重新全文注入核心文档与完整服务OpenAPI。
- 可选模型工作线程，角色默认不启用付费调用，必须显式配置。

定制任务API和当前处理边界见 [docs/custom-tasks.md](docs/custom-tasks.md)。调度器默认启用，可用 `AI_OPS_SCHEDULER_ENABLED=0` 禁用调度线程。

## 明确尚未实现

OpenClaw 集成（已定：不自研 fork，见 docs/self-built-route.md）、模型生成的**高质量棗概**（按天分层记忆已实现，见 docs/memory.md）、Skills、成熟告警诊断策略、Web、微信/飞书仍需完成。原始输出归档与运行中取消已在协议 1.1 实现，但仅限声明 1.1 的 Agent；`POST /tasks` 仍是管理员单命令入口；模型聊天使用 `/api/v1/roles/{id}/messages`，模型只能通过受控工具创建该轮次的子任务。SSH Connector、输出保留配额、备份恢复、输出/审计的敏感信息脱敏、以及凭据轮换（资产 token 轮换 + admin token 热轮换）已实现（见 `docs/` 下对应文档）。

模型配置与边界见 [docs/model-turns.md](docs/model-turns.md)，执行协议 1.1（心跳、取消、原始输出归档）见 [docs/protocol-v11.md](docs/protocol-v11.md)，Agent 安装与升级见 [docs/operations.md](docs/operations.md)，租约与未知执行处置见 [docs/leases.md](docs/leases.md)。不自动把提示词当命令；模型请求启用非流式响应，遇到不支持的响应或未知执行状态明确停止。

主服务与 Agent 暂以 Python 实现以尽快验证执行契约；**产品主体自研，不 fork OpenClaw**，但借鉴其渠道抽象/上下文引擎/凭据边界/可插拔 provider 的设计（见 [docs/openclaw-assessment.md](docs/openclaw-assessment.md) 与 [docs/self-built-route.md](docs/self-built-route.md)），执行协议不依赖未来模型编排实现。联网搜索的 provider 取舍、SearXNG 默认与合规边界见 [docs/search.md](docs/search.md)。商城本期不开发，后续保留服务边界。

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

push/PR 运行 Python 3.11/3.12 测试。测试通过后构建镜像；非 PR 推送 `ghcr.io/hewenze11/ai-ops`，以分支、提交 SHA、版本标签区分。使用 Actions 内置 GITHUB_TOKEN，不需把个人 PAT 存入工作流。没有自动 SSH 部署，避免每次提交直接改测试机。

许可证尚待项目所有者确定；暂不附加未经确认的开源许可证。
