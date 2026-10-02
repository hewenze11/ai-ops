# AI Ops 主服务

轻量运维工作台。**当前是 0.1.0.dev2 后端预览，不是已完成产品，也不建议暴露公网。**

## 本次实现

- FastAPI + SQLite 的认证 API，独立资产凭据（数据库仅存其哈希）。
- 角色、资产备注、每轮账号名单快照、逐操作实际账号。
- 事务内的角色 FIFO 队列、确认后入队、未领取任务取消。
- Agent 协议 1.0：领取任务、回传结果、幂等提交与结果重试。
- 状态变更与审计同事务落盘；未知执行不自动重跑。
- Docker / Compose、GitHub Actions 测试和 GHCR 镜像构建。
- 「定制任务」后端：触发任务与定时任务共用提示词、角色、账号及执行模式配置。
- 五段Linux风格Cron、IANA时区、未来触发时间预览；定时器通过HTTP调用统一trigger接口。
- 持久化定时投递箱、重试去重、配置快照、暂停/删除保留历史；输入进入待模型处理队列。

定制任务API和当前处理边界见 [docs/custom-tasks.md](docs/custom-tasks.md)。调度器默认启用，可用 `AI_OPS_SCHEDULER_ENABLED=0` 禁用调度线程。

## 明确尚未实现

模型对话和工具循环、OpenClaw 集成、完整角色记忆、Skills、告警后的AI排障、SSH Connector、Web、微信/飞书、运行中取消、凭据轮换、未知状态人工处置接口、自动恢复/备份工具、完整输出附件存储、模型输出脱敏。当前 `POST /tasks` 是管理员手动提交执行指令的联调入口，不是模型聊天接口。定制任务的提示词已按角色持久化排队，但尚无模型消费者，不会自动解释为命令或伪称已完成AI处理。

主服务与 Agent 暂以 Python 实现以尽快验证执行契约；这不代表已经完成 OpenClaw 选型，执行协议不依赖未来模型编排实现。商城本期不开发，后续保留服务边界。

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
6. 输出每个流暂限 64KiB 并明确标记截断；这是已知预览缺口，不符合最终版全量执行输出归档目标。
7. 目前记录任务与输出原文，尚未实现通用敏感输出清理。仅用于可控测试，不要在任务中传入秘密；不得让低权限用户读取数据库。
8. 主服务不以 root 运行，Compose 丢弃 capabilities、只读文件系统。资产系统用户权限及平台部署安全仍由操作者配置。
9. 普通进程组清理不等于 cgroup 级后代清理；不可宣称支持任意守护进程的可靠停止。

## CI 与发布

push/PR 运行 Python 3.11/3.12 测试。测试通过后构建镜像；非 PR 推送 `ghcr.io/hewenze11/ai-ops`，以分支、提交 SHA、版本标签区分。使用 Actions 内置 GITHUB_TOKEN，不需把个人 PAT 存入工作流。没有自动 SSH 部署，避免每次提交直接改测试机。

许可证尚待项目所有者确定；暂不附加未经确认的开源许可证。
