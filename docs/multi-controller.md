# 多主服务联邦（multi-controller）

一个执行 Agent 可以被**多个主服务**同时接管。本文说明协议、配置与边界。

## 目标

1. 一台受控机器可被多个主服务（多人/多团队）操控。
2. 执行端本机配置里可以**逐个禁用**某个主服务——禁用即等于拉黑，立即生效。
3. 同一资产**同一时刻只执行一条命令**（执行级互斥由 Agent 保证）。
4. 其它主服务通过心跳感知"正忙"，**自己**把任务留在队列等待。

## 不做的事（明确边界）

- **不做业务级优先级/抢占仲裁**：谁的运维操作更该先做，是人的事。
- **不做主服务之间的协调**：各主服务相互独立，不知道彼此存在。
- **不替用户解决"同一主服务开多角色往同一台机灌命令"**：这是用户自毁长城，
  与 Linux 允许多人同时登录同理，产品不拦。

## 关键事实（改造前已具备）

- 主服务侧 `claim` 一次只捞一条（`LIMIT 1`），状态置 `claimed`。
  因此**同一资产上的执行天然串行**——多角色并发下令、执行层自动排队，无需新增锁。
- Agent 与主服务之间是**无状态短 HTTP 请求**（心跳/claim/control/result），
  没有可供互斥的长连接。

## 配置

Agent 配置新增可选的 `controllers` 数组；`server_url` / `agent_token` 保留为
单主服务的兼容写法。

```json
{
  "asset_id": "video-01",
  "allowed_users": ["ops"],
  "journal_dir": "/var/lib/ai-ops-agent",
  "controllers": [
    {"name": "team-a", "url": "https://ops-a.example.com", "token": "…", "enabled": true},
    {"name": "team-b", "url": "https://ops-b.example.com", "token": "…", "enabled": false}
  ]
}
```

- `enabled: false` 的主服务被**完全跳过**：不发心跳、不接受其任务。
  这就是本机对某个主服务的**阻断**开关。
- 每个 controller 的 `token` 独立，互不可代用。

## 协议（1.2）

心跳增加忙状态字段：

```json
{
  "instance_id": "…",
  "agent_version": "0.1.0.dev3",
  "protocol_version": "1.2",
  "busy": true,
  "busy_by": "team-a",
  "busy_task": "task-uuid"
}
```

主服务收到 `busy=true` 且 `busy_by` 不是自己时，**claim 返回空**，
任务留在 `queued`；等忙状态解除后自动可领。

## 执行级互斥

Agent 主循环单线程，`agent.lock` 保证一个 journal 只有一个进程——
同一时刻只执行一条命令。跨主服务亦然。

## 兼容性

- 协议 1.0 / 1.1 Agent 无 `busy` 字段：主服务按"不忙"处理（保持旧行为）。
  这意味着多主联邦的忙感知需要 1.2 Agent。
- 单 controller 配置与旧版行为完全一致。
