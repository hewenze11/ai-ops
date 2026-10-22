# AI 决策选择卡（2026-10-09）

## 使用流程

AI 遇到必要业务判断或缺少关键信息时，调用 `ask_user`；无关键决策不应逢事提问。一次只调用一个工具、一个单选问题：

```json
{"name":"ask_user","arguments":{"question":"本次采用哪种部署方案？","options":[{"id":"keep","label":"保留现有架构","description":"改动最小"},{"id":"migrate","label":"迁移至新方案","description":"需要另行确认执行命令"}]}}
```

平台始终提供“其他”及自定义文本，模型不应自行添加 `other`。模型工具只创建问题，管理员接口只能回答已经存在的对应问题，不能通过模型参数改变执行权限。

聊天中卡片在运行详情之外显示，单选后“提交选择并继续”。选择“其他”必须填写文本。刷新后待答卡和已答摘要从服务端恢复；同一页自动刷新保留未提交草稿。退出/关闭页面不会持久保存未提交草稿，已提交答案持久保存。聊天读取最近100轮，并额外取同角色待答轮次，避免待答卡被大量后续排队消息挤出。

## API 与持久化

- `GET /api/v1/turns/{turn_id}`、`GET /api/v1/roles/{role_id}/turns`：含 `questions`。记录 `id/turn_id/role_id/tool_call_id/question/options/state/answer/created_at/answered_at`。
- 列表默认原有升序分页不变；`latest=true` 取最近 `limit` 条（返回仍升序），`waiting_user=true` 筛待答轮次。
- `POST /api/v1/turns/{turn_id}/questions/{question_id}/answer`，管理员 Bearer 鉴权。

```json
{"option_id":"keep","text":""}
```

或：

```json
{"option_id":"other","text":"先给出两种方案的成本对比，不执行变更"}
```

成功返回 `question_id/state/duplicate`。同问题相同规范化答案幂等；不同二次回答409；非法选项/空其他/超长422；跨轮问题404；无授权401；已取消或终态待答问题409。已答重复请求只回执，不唤醒已终态轮次。

`turn_questions` 与 `role_turns.messages/state` 在同一个 SQLite BEGIN IMMEDIATE 事务更新，保证并发回答只追加一次 tool result。回答作为原 `tool_call_id` 的普通 tool message 注入下一次原轮次模型调用，JSON标记 `source=user_input`、`grants_execution_approval=false`；不是新会话，也不是 system 指令。

## 等待、计费与边界

- 轮次 `waiting_user` **不是完成**。worker不为等待轮次调用模型、不占住模型调用线程，不消耗额外模型步数。继续阻塞同角色后续轮次，不阻塞其他角色。
- 不设置自动短截止。服务重启保留待答问题；回答后 `ready` 可继续。已有实际模型调用仍计入该角色配置的步数上限，达到上限后恢复将明确失败 `MODEL_STEP_BUDGET_EXHAUSTED`；默认 `null` 不限。
- 1–8个模型选项；ID 1–64字符（字母数字开头，可含下划线/短横线），区分大小写但所有大小写的 other 保留；ID不可重复。问题最多2000字，label 200，description 500，自定义回答4000。动态内容使用textContent，不解析HTML。
- `readonly/confirm/direct` 均可问。**决策不是命令审批**：readonly仍拒绝execute_command；confirm后续命令仍awaiting_approval；选项无法改变mode/execution_users/run_as授权。
- 卡片“取消整轮任务”是硬取消；普通选项即使叫“取消方案”，提交仍是交给AI的答案，两者不同。
- 渠道暂不原生呈现交互卡。`/api/v1/channels/outbox` 新增独立 `waiting_user` 数组，供桥接展示“打开Web聊天回答”的引导及 `#turn=...`；不放进完成消息、不推进最终回复cursor。旧桥接若未读取此字段不会自动发引导，用户仍须进入Web；不能声称飞书/微信已支持原生选择卡。
- 平台仍是全局管理员信任域，不新增租户/RBAC隔离。没有多选、批量问题、自动超时、回答编辑、原生渠道卡。

## 验证

`tests/test_questions.py` 使用真实FastAPI/SQLite、fake model与真实worker线程：等待/恢复/原tool_call_id、三模式、其他、取消方案、FIFO、跨角色释放、重启、幂等/并发/取消竞争、终态、权限、模型上限、分页和渠道cursor。`console/console.test.cjs` 调用真实前端渲染/提交事件，核对payload、草稿恢复、已答摘要、禁重复、XSS文本与硬取消；并保留notes和资产注销显示回归。它是DOM自动测试，不代替用户真实浏览器体验。
