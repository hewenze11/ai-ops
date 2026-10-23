# 阶段 6 推进记录（承接上一轮）

上一轮做完三件事后，用户要求"继续做，把前端以外的东西准备好"。本轮完成了原待办①（解锁真回显），并顺手堵了一个参数夹带的洞。

## 为什么 `revealed` 之前是假的

最初实现里 `GET .../revealed` 从 `tasks.result` 里取 stdout 返回。但 `report_result` 对所有结果无条件调 `scrubbing.redact_result`（因为那段文本要作为工具输出交给模型）。所以解锁拿到的永远是 `[REDACTED]`。功能存在，但做不了它唯一该做的事。

## 修法（四处联动，缺一不可）

1. **Agent 新增独立 kind `reveal`**，不是在 readonly 上加 `reveal=True`。原因：flag 由构造 readonly payload 的一方决定，而那一方拿的是模型输入。独立 kind 把决定权放在只有管理员接口能调用的代码路径上。
2. **`report_result` 对 reveal 开唯一例外**，不脱敏，但**不写 `tasks.result`**，改写新表 `reveal_secret`。`tasks.result` 就是模型看到的工具输出，写进去等于悄悄把明文交给模型。
3. **`revealed` 改两态**：首次请求派发 clone 任务返回 202，授权**不消耗**（派发不等于看到）；拿到字节才销（删 stash + 改状态）。
4. **`authority.claim_allowed` 给 reveal 单开分支**。reveal 的 `turn_id` 是 NULL（操作员派发，不属于任何模型轮次），走不了原有 turn 校验，改用 `source_turn_id` 复核原读的授权 + 15 分钟新鲜度。

## Agent 侧的额外收紧

- reveal 只允许 `read_file`。目录列表或 journal 全量解锁是另一个功能、另一个威胁模型，直接拒绝而不是悄悄截断。
- root 一律拒绝，**不提供** `allow_root_readonly` 旁路。root 能读全机私钥，而这条路径返回不脱敏字节。
- 输出上限 256KB；回传"正常读会脱敏几段"的计数，让操作员看到明文的**同时**知道它长什么样像密钥。
- 不回传 credentials 句柄：值是操作员视图，注册句柄等于"看了一眼就入库"。

## 排队规则

reveal **不插队**：只在没有模型任务可领、且没有在飞任务时才被领取。一个人盯着解锁卡片，不该拖慢正在诊断的那一轮。

## 顺手堵的一个洞

`turns.py` 的 `clean_params` 原样拷贝模型传入的**任意**键进 payload。`kind` 目前是派发器硬编码的，所以不可利用；但"模型能写的字段"和"派发器写的字段"同名，一次"合并 payload"的重构就能让模型自己选任务类型。已加 `_RESERVED` 黑名单拦截。

## 验证

- 主服务 652 passed（+3）
- Agent 111 passed / 35 skipped（+5）
- 新增 `test_reveal_bytes_never_land_in_the_task_result`：直接断言明文不在 `tasks.result`、在 `reveal_secret`、取回后该行被删。

## 剩余

- ①`read_sanitized` 中间层：前置条件是"先加谓词工具就要先做"，当前工具集无谓词，不急。
- ②前端：等用户给模板后由 Opus 重做。
