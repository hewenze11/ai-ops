# 治理控制台（Governance Console）— 前端接口清单

> 本文是给前端实现者的**唯一输入**。设计权完全放开：信息架构、视觉、交互、文案自行决定。不要参考仓库内 `ai_ops/console/` 下的现有页面——那是旧的、将被替换的工作台，不构成任何风格约束。

## 0. 这是什么

一个运维 AI 平台。AI（大模型）通过"角色"与管理员对话，对受管服务器做只读排查或提出变更。平台的核心卖点是：**AI 的每一步都可审计、可把控**。本页面就是管理员"把控"的地方。

管理员的心理模型只有一个：**"AI 停下来等我点头了"**。所有需要人决定的事进一个收件箱；其余三块（名单与模式、凭证库、审计时间线）是支撑这个决定的上下文。

目标用户：中级运维工程师，单管理员（开源版无多用户）。桌面优先，移动端可用即可。

## 1. 交付形态

- 纯静态：一个 HTML 入口 + CSS + JS，**不引入任何第三方运行时依赖**（无框架、无 CDN）。构建期工具随意，但产物必须是可直接由后端静态托管的文件。
- 产物放到 `ai_ops/console/governance/` 目录：`index.html`、`app.css`、`app.js`（文件名固定，后端按此托管；若需拆分多文件告知）。
- 页面路由：后端在 `/governance` 托管 `index.html`，静态资源在 `/governance/assets/<rev>/<file>`；HTML 内用占位符 `__AI_OPS_UI_REVISION__` 引用资源版本（后端替换）。
- 中文界面。

## 2. 认证

所有 `/api/v1/*` 请求带 `Authorization: Bearer <admin_token>`。token 由管理员手工输入，存 `sessionStorage`（不要 localStorage）。401 → 回到输入 token。没有登录页概念，只有一个 token 输入。

所有时间字段都是 Unix 秒（float）。

## 3. 收件箱（核心，优先做）

### 3.1 列表

`GET /api/v1/inbox`

```json
{
  "approvals": [ {...审批卡} ],
  "questions": [ {...问题卡} ],
  "account_requests": [ {...账号申请卡} ],
  "total": 3,
  "note": "..."
}
```

三种卡片**混排在一个列表**里（按 `created_at` 排序由前端决定），每种卡片的"决定动作"不同。轮询刷新（建议 5s），无 WebSocket。

#### 审批卡 `kind: "approval"`

AI 提出了一个变更集，策略引擎判定需要人批准。

```json
{
  "kind": "approval",
  "task_id": "uuid", "turn_id": "uuid",
  "asset_id": "web1", "asset_name": "生产 Web-1",
  "role_id": "ops", "role_name": "值班运维",
  "run_as": "ops_deploy", "mode": "confirm",
  "steps": [
    {"action": "service.reload", "program": "/usr/bin/systemctl", "args": ["reload", "nginx"],
     "params": {"unit": "nginx"}, "timeout_seconds": 60}
  ],
  "payload_sha256": "64位hex",
  "reason": "AI 写的理由：证书续期后需要 reload",
  "prompt": "触发这轮对话的用户原话（最多500字）",
  "caller": "web | feishu | trigger:xxx",
  "created_at": 1760000000.0,
  "command": null
}
```

- `steps` 是**将要真正执行的字节**：`program + args` 就是 execve 的参数，没有 shell。这是卡片最重要的内容，要让人一眼看清"在哪台机、以哪个账号、跑什么"。
- `payload_sha256` 是执行端复核用的哈希，显示出来（可折叠/缩略），意义是"你批的就是这串，执行端会校验一模一样"。
- `command` 仅老格式任务非空（此时 `steps` 为空），兼容显示即可。
- 多个 step 是**原子**的：要么全批要么全拒，没有逐条勾选。

决定：
- 批准 `POST /api/v1/tasks/{task_id}/approve`，body 可选 `{"reason": "备注"}` → `{"state":"queued"}`
- 拒绝 `POST /api/v1/tasks/{task_id}/reject`，body `{"reason": "拒绝理由"}` → `{"state":"rejected"}`
  - **拒绝理由会原样回给 AI**，AI 据此改方案。界面上要让人知道这一点（理由是写给 AI 看的，不是写给日志的）。
- 409 = 已被处理（并发/过期），刷新列表即可。

#### 问题卡 `kind: "question"`

AI 问一个业务单选题（不是授权，只是信息）。

```json
{
  "kind": "question",
  "question_id": "uuid", "turn_id": "uuid",
  "role_id": "ops", "role_name": "值班运维",
  "question": "切哪个环境？",
  "options": [{"id": "prod", "label": "生产", "description": "可选说明"}, ...],
  "created_at": 1760000000.0
}
```

决定：`POST /api/v1/turns/{turn_id}/questions/{question_id}/answer`
- 选项：`{"option_id": "prod"}`
- 自由输入：`{"option_id": "other", "text": "自定义回答"}`（平台总是允许"其他"，前端要提供这个入口；`text` 只在 other 时允许且必填）
- 409 = 已回答或轮次已结束。

#### 账号申请卡 `kind: "account_request"`

AI 请求在本轮对话中获得某台机某个 OS 账号的执行权。

```json
{
  "kind": "account_request",
  "request_id": "uuid", "turn_id": "uuid",
  "role_id": "ops", "role_name": "值班运维",
  "asset_id": "db1", "asset_name": "生产 DB",
  "username": "dba", "reason": "需要重启数据库",
  "mode": "confirm",
  "revision": 0,
  "created_at": 1760000000.0
}
```

决定：`POST /api/v1/turns/{turn_id}/account-requests/{request_id}/answer`，body `{"decision": "approve" | "deny", "revision": <卡片里的 revision>}`
- `revision` 必须回传卡片里的值；409 = 过期/已答，刷新。
- 批准后该账号仅在**这一轮**有效，文案要说清。这不是批准任何命令——命令另走审批卡。

### 3.2 上下文

卡片上可提供"查看对话"入口：`GET /api/v1/turns/{turn_id}` 返回完整轮次（`prompt`、`messages[]`、`state`、`steps`、`questions`、`account_requests`、`execution_grants` 等）。`messages` 是 OpenAI 风格消息数组（role: system/user/assistant/tool），`assistant` 的 `tool_calls[].function.{name,arguments}`，`tool` 消息的 `content` 是 JSON 字符串。只需能展开看，不必做聊天 UI。

## 4. 名单与模式

### 4.1 概念（必须让用户理解）

- **模式**（mode）三选一，每轮对话开始时固定：`readonly`（只读排查）、`confirm`（提议变更，人批）、`direct`（白名单内自动执行，其余人批）。
- **名单**（name list）是**不可变快照**：有 `id`、`kind`（`readonly | write | deny`）、`version`、`parent_id`（fork 自哪个）。想改 = fork 出新版本再绑定，**不能原地编辑**。原因：正在执行的任务冻结了它开始时的名单 id，原地改会让"审批时看到的"和"执行时用的"不是一份。
- **绑定**（binding）：每个模式绑哪三张名单。改绑定只影响之后开始的新任务。

### 4.2 接口

- `GET /api/v1/name-lists` → 名单数组（未退役的），每项：
  ```json
  {"id": "...", "kind": "write", "name": "默认写名单", "version": 3, "parent_id": "…或null",
   "created_at": ..., "retired_at": null,
   "rules": [{"rule_id": "service.reload", "action": "service.reload", "hosts": ["tag:prod"], "note": "说明"}]}
  ```
  - `hosts` 为空数组 = 全部主机；否则是主机标签列表（如 `tag:prod`），规则只在带这些标签的主机上生效。
- `GET /api/v1/name-lists/{id}` → 单个。
- `POST /api/v1/name-lists/{id}/fork` body `{"name": "新名字（可选）", "rules": [...]（可选，省略则复制）}` → 201 新名单。
- `GET /api/v1/name-list-bindings` → `{"readonly": {"readonly_list": id, "write_list": id|null, "deny_list": id|null}, "confirm": {...}, "direct": {...}}`（缺某模式 = 用平台默认）。
- `PUT /api/v1/name-list-bindings/{mode}` body `{"readonly_list": id|null, "write_list": id|null, "deny_list": id|null}` → 返回全部 bindings。
- `PUT /api/v1/assets/{asset_id}/tags` body `{"tags": ["prod", "web"]}` → `{"asset_id", "tags"}`；资产列表见 `GET /api/v1/assets`（每项含 `id`、`name`、`allowed_users[]`、`tags[]`、`connection_type`、`health`）。

需要的交互：看每张名单的规则；两张名单（父子）**对比 diff**；fork（可编辑规则后提交）；切换绑定。不做"规则在线编辑器"——fork 表单里编辑是允许的，因为那是在创建新快照。

## 5. 凭证库

### 5.1 概念

Agent 在服务器上读到密钥样子的内容时，**不回传明文**，而是在服务器本地加密存下并只上报一个"句柄"（id + 名字 + 发现位置）。控制面**没有任何接口能返回明文**。本页是**清单**不是保险箱——"在 4 台主机上发现 17 处明文密钥，已托管"。

### 5.2 接口

- `GET /api/v1/credentials?asset_id=&include_dismissed=false` →
  ```json
  {"items": [{"id": "cred-xxx", "asset_id": "web1", "name": "unnamed-3", "note": "", "source": "/opt/app/.env",
              "kind": "pattern|path|file|connector", "created_at": ..., "last_seen_at": ..., "dismissed": 0, "dismissed_reason": null}],
   "counts_by_asset": {"web1": 5}, "note": "Handles only; values stay on the managed host."}
  ```
  - `kind`: `pattern` 正则命中片段（可能误报，如 git sha）；`path` 路径规则命中整文件；`file` 文件凭证；`connector` 无 Agent 的降级路径（值经过控制面，要标出来）。
- `PUT /api/v1/assets/{asset_id}/credentials/{id}` body `{"name": "...", "note": "..."}`（任一可省）→ 更新后的句柄。`source`/`kind`/`id` **不可改**（传了会 422）——它们是证据。
- `POST /api/v1/assets/{asset_id}/credentials/{id}/dismiss` body `{"reason": "不是密钥"}` → 误报否决，之后不再上报。
- `GET /api/v1/assets/{asset_id}/credentials/{id}/history` → `[{"seq", "event": "discovered|annotated|dismissed", "actor", "detail"(JSON字符串), "created_at"}]`

**硬约束**：界面任何地方不得出现"查看明文/复制值"之类的入口，哪怕灰掉也不要。

## 6. 审计时间线

`GET /api/v1/audit?limit=100&before=<seq>|after=<seq>&entity_id=&event_prefix=&actor=`

- 返回 `[{"seq", "event": "task.approved", "entity_id", "actor": "admin|service|model-tool|policy-engine|agent:…", "details": {...}, "created_at"}]`
- `before` 倒序（时间线从新到旧翻页用），`after` 正序。
- `event_prefix` 字面前缀，如 `task.`、`change.`、`credential.`、`name_list.`、`authority.`、`question.`。
- `entity_id` 精确匹配：传 task_id / turn_id / asset_id / list_id 都可以。

只读视图。常用入口：从审批卡 → 该 turn/task 的时间线；从凭证 → 该资产的时间线；从名单 → 该名单的事件。

## 7. 辅助接口

- `GET /api/v1/roles` → `[{"id","name",...}]`
- `GET /api/v1/assets` → 资产数组
- `GET /api/v1/operator/attention` → `{"unknown_executions":[], "awaiting_approvals":[], "stale_leases":[], "offline_assets":[]}`（可用于顶部红点；`awaiting_approvals` 与 inbox 重复，取 inbox 为准）
- `GET /healthz` → 200

## 8. 明确不做

- 聊天界面、资产注册、角色配置、告警、巡检、记忆、Skills——都不在本页。
- 任何"直接执行命令"的入口。
- 多用户、权限分级。
- 名单原地编辑。

## 9. 验收方式

用户亲自在浏览器审（体验优先于功能完整）。请提供：产物文件 + 一份 ≤30 行的 `README.md`（设计取舍、如何本地预览、已知限制）。不需要写测试框架，但 `node --check app.js` 必须通过。
