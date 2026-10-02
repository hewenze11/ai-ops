# 执行协议 1.1（当前）

协议 1.0 见 [protocol-v1.md](protocol-v1.md)，仍然可用；1.1 在其上增加心跳、运行中取消与原始输出归档。服务端对同一资源同时接受两种版本，Agent 端精确匹配 1.1。

## 为什么保留两个版本

1.0 只支持“领取—执行—回传结果”，无法在任务执行中表达取消，也没有独立的原始输出通道。1.1 不修改 1.0 的任务/结果字段语义，只增加新端点，旧 Agent 在升级窗口内仍可工作；服务端不会因为一个资产上报 1.0 就给它派发 1.1 专属能力。

版本在 `claim` 请求体中协商，服务端把协商结果写入该任务的 `claimed_protocol`。取消、归档都必须由 1.1 声明的任务使用，不能事后补声明。

## 新增端点

所有端点使用资产 Token（`Authorization: Bearer <agent_token>`），只允许访问本资产的任务；不匹配一律 403。

| 端点 | 用途 |
| --- | --- |
| `POST /api/v1/agents/{asset_id}/heartbeat` | 上报实例、Agent 版本与协议版本，服务端记录最后在线时间 |
| `POST /api/v1/agents/{asset_id}/tasks/{task_id}/control` | 查询取消请求（Agent 心跳期间轮询） |
| `POST /api/v1/agents/{asset_id}/tasks/{task_id}/output/{stdout\|stderr}/chunks` | 上传原始字节分块，base64 编码，块 ≤ 64 KiB |
| `POST /api/v1/agents/{asset_id}/tasks/{task_id}/output/{stdout\|stderr}/finalize` | 提交该流的总长度、SHA-256 与完整标记 |

管理端新增（`Authorization: Bearer <admin_token>`）：

| 端点 | 用途 |
| --- | --- |
| `GET /api/v1/agents/{asset_id}/status` | 在线状态、最近实例、未完成任务 |
| `GET /api/v1/tasks/{task_id}/output` | 归档清单（长度、摘要、是否完整） |
| `GET /api/v1/tasks/{task_id}/output/{stdout\|stderr}?offset=&limit=` | 按字节分页下载原始输出 |

## 心跳

- 字段：`instance_id`（进程内随机）、`agent_version`、`protocol_version`（必须 `1.1`）。
- 30 秒无心跳视为离线；离线只是观测信息，**不会**把已领取任务改回队列，也不会解除同角色阻塞。
- 重连（实例号变化或超过 30 秒）写入 `agent.connected` 审计。

## 运行中取消

取消是请求 + 确认，不是服务端单方面终止：

1. 管理员 `POST /api/v1/tasks/{id}/cancel`；未领取任务直接取消。
2. 已领取任务必须由 1.1 Agent 领取，否则返回 409（本预览不伪造远程终止）。通过后任务状态不变，写入 `cancel_requested_at`，审计 `task.cancel_requested`。
3. Agent 每隔约 2 秒轮询 `control`，看到 `cancel_requested` 后杀掉整个进程组；取消前也会先查一次，避免启动即被取消的命令。
4. Agent 回传 `status=cancelled`，`error_code=CANCELLED_BY_OPERATOR`。未请求取消的任务上报 `cancelled` 会被拒绝（409）。
5. 取消与完成竞态时，已产生的真实结果保留，只把角色轮次置为 `cancelled`，不会显示成“已取消”而掩盖结果，也不会继续让模型跑后续命令。
6. 断连不等于已停止：Agent 与服务失联时仍按本地超时执行并保留结果，恢复后按幂等规则补交。

`unknown` 状态的任务不能被取消解除，必须人工处置——取消不能替未知执行下结论。

## 原始输出归档

- 每个流（stdout/stderr）单独归档，上限 64 MiB，超出即 `complete=false`，绝不把截断结果标成完整。
- 分块必须从 0 开始连续；断线重传同一 offset 的相同字节幂等通过，不同字节返回 409。
- `finalize` 校验“已接收长度 == 声明长度”且“重算 SHA-256 == 声明摘要”，通过后归档冻结，不可再追加或改写。
- 结果回传可引用归档：`output_archives` 必须是 `{"stdout": {...}, "stderr": {...}}`，字段与该任务已冻结的归档完全一致，否则 409。这样“结果被接受”与“原始输出确实完整落盘”绑定，不能只报摘要。
- Agent 上传前会重新读取本地文件核对摘要；文件被改动即停止提交（不重放命令）。
- 归档失败写入 `complete=false`（磁盘错误、读取错误、进程组未完全结束等）。低层 `stdout`/`stderr` 文本字段仍保留 64 KiB 预览，便于模型快速判断。

## 兼容与迁移

- 服务端 schema 版本 4 新增 `agent_presence`、`output_archives`、`output_chunks`，并为 `tasks` 增加 `claimed_protocol`、`cancel_requested_at`。升级保留原有任务、轮次、审计记录。
- 1.0 Agent 不受影响；管理端对 1.0 已领取任务请求取消会得到明确 409，而不是静默无效。
- 已冻结归档不会因服务重启丢失；重启后 Agent 可用同一 claim 继续上传或重试最终结果。
