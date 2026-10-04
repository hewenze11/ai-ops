# 任务租约与未知执行处置

本页描述当前实现（服务端 schema 5）。核心原则：**租约只是观测，未知执行只能由人下结论。**

## 任务租约

Agent 每次领取任务都会开一条租约：

- 租约字段：`lease_id`（等于 `claim_id`）、`claimed_at`、`expires_at`、`closed_at`。
- 有效期 `LEASE_SECONDS = 300`。
- 任务查询会返回租约状态：`expired`、`closed`，以及 `claim_count`、`lease_seconds`。
- 结果回传、取消、人工处置都会关闭租约。

### 为什么过期不自动重派

自动重派意味着把一条可能已经在目标机上跑过的命令再发一次。对 `rm`、`systemctl restart` 这类操作，重派的代价远高于"卡住等人看一眼"。所以当前实现：

- 租约过期只出现在 `GET /api/v1/operator/attention` 的 `stale_leases` 里，并明确标注"未重派"；
- 任务保持 `claimed`，同角色后续任务继续排队；
- 想继续必须由人处置：确认机器实际状态后，用下面的未知处置接口，或（如果确实从未执行）后续再设计一条显式的"确认未执行并重排"路径。

也就是说：**租约解决的是"看得见卡在哪"，不是"自动帮你重试"。**

## 未知执行处置

`GET /api/v1/operator/attention`（管理员）汇总三类需要人看的东西：

```json
{
  "unknown_executions": [{"id": "...", "asset_id": "...", "role_id": "...", "payload": {}, "updated_at": 0}],
  "stale_leases": [{"task_id": "...", "asset_id": "...", "state": "claimed", "expired_for": 12.3, "note": "..."}],
  "offline_assets": [{"asset_id": "...", "silent_for": 41.0}]
}
```

处置接口（管理员）：

```
POST /api/v1/tasks/{task_id}/resolve
{"action": "confirm_succeeded" | "confirm_failed" | "abandon",
 "note": "至少3个字符的说明", "confirm_task_id": "<同一个 task_id>"}
```

- `confirm_succeeded`：人已核实执行成功 → 任务 `succeeded`，租约关闭，角色解除阻塞。
- `confirm_failed`：人已核实执行失败 → 任务 `failed`，角色轮次 `failed` 并带 `OPERATOR_CONFIRMED_FAILED`。
- `abandon`：**不声称成功也不声称失败** → 任务 `abandoned`，`result.status` 仍是 `unknown`，角色轮次 `completed`（解除阻塞），审计记录 `EXECUTION_ABANDONED_UNVERIFIED` 的语义通过 `task_controls` 与审计体现。这是"查明不了、但不想永远卡住"时的诚实出口。

规则：

- 只有 `unknown` 状态可以处置，其它状态返回 409；
- `confirm_task_id` 必须与路径一致，否则 422；
- `note` 至少 3 个字符（题目与审计都需要人话）；
- 处置写入 `task_controls`（逐条操作流水）与 `audit`（全局审计），actor 为 `admin`；
- 资产凭据读不到处置流水与 attention 列表（403）。

查询单个任务的处置记录：`GET /api/v1/tasks/{task_id}/controls`。

## 与取消的区别

| | 取消 | 未知处置 |
| --- | --- | --- |
| 适用状态 | queued / awaiting_approval / claimed（需 Agent 确认） | 仅 unknown |
| 断连时 | 不保证已停止 | 不适用 |
| 结论 | 明确"已请求停止" | 人给结论或明确搁置 |

`unknown` 不能用取消解除：取消不能替一个结果不明的执行下结论。

## 积压风险与恢复建议

`unknown` 会**阻塞整条角色队列**（同一角色后续任务不执行）。如果一个角色连续出现多个 `unknown`，恢复流程偏重。建议：

- 尽快用 `GET /api/v1/operator/attention` 巡一次 `unknown_executions`，逐个处置；
- 对“确实无法确认”的先 `abandon` 解除阻塞（不谎报成败），避免队列长期积压；
- 这是**有意的安全取舍**：宁可卡住等人看一眼，也不自动重跑可能已执行过的变更命令。若高可用场景对阻塞时间敏感，需要的是“显式确认未执行并重排”的新通路（尚未实现）。
