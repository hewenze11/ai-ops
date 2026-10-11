# 模型驱动的角色轮次（dev3预览）

## 角色轮次与执行命令分离

`role_turns` 是统一的角色工作队列。聊天消息、定制任务的trigger输入、定时输入，以及管理员直接提交的单命令，都占据该队列中的一个位置。一个模型轮次可以依次产生多条Agent命令；即使命令创建时间晚于后续人工任务，也不能让后续轮次插进来。

同角色串行；不同角色由最多4个模型工作线程并行推进。数据库事务负责领取轮次，不能同时有两个模型调用处理同一角色的不同轮次。数据库不支持多个主服务实例的主动/主动部署：应用启动时会将中断的calling状态标记失败，不自动重试付费请求。

Agent协议仍为1.0；Agent不调用模型、不需要模型Key。新增逻辑不要求升级Agent。

## 模型传输与凭据

当前使用OpenAI兼容 `/chat/completions`，要求受信任HTTPS基础地址。Key从只读秘密文件加载，在请求发出时加入认证头；不进入工具参数、数据库请求正文、命令子进程环境或模型上下文。拒绝带凭据的重定向、URL内嵌账号密码及环境代理。

请求显式 `stream=false`、JSON Accept 和 User-Agent，验证返回结构。不支持的多工具并行响应、截断响应或空响应均停止并记录错误，不猜测补全。这里只记录接口实际返回的普通assistant内容、tool调用和usage；不请求或伪造隐藏推理。

可选Compose覆盖文件：

```sh
# 凭据应通过受控方式写入secrets/model_key，而不是贴到命令参数。
# 文件需让容器UID10001可读，权限0600，Git忽略。
# 注意：该文件**必须先作为文件存在**再启动 compose；若路径缺失，Docker 会把
# bind-mount 目标建成**目录**，容器随即启动失败（exit 127）。
export AI_OPS_MODEL_BASE_URL=https://your-provider.example/v1
export AI_OPS_MODEL=your-verified-model-id
docker compose -f compose.yaml -f compose.model.yaml -p ai-ops-preview up -d
```

模型工作线程默认启动，以支持完全不配置全局接口、仅配置独立角色接口；可用 `AI_OPS_MODEL_WORKER_ENABLED=0` 关闭。线程启动不自动启用角色：API角色模型默认enabled=false，前端新建时明确显示启用复选框。已有角色开关不改，不补跑被禁用角色的旧队列。

## 角色模型与消息API

`PUT /api/v1/roles/{role_id}/model`（管理员）：

```json
{
  "enabled": true,
  "model": "",
  "provider_mode": "inherit",
  "max_model_steps": null,
  "max_output_tokens": 2048,
  "max_context_chars": 250000
}
```

`provider_mode=inherit` 时model为空继承全局模型、非空只覆盖名称。`provider_mode=independent` 时传 `base_url`、`model` 和首次写入的 `api_key`，独立支持 OpenAI 兼容 `/chat/completions`，不支持原生Claude/Gemini协议。地址限公网HTTPS默认443端口，无账号密码/查询/片段；每次连接解析DNS、拒绝私网/回环/元数据并固定到检查过的IP，TLS仍验证原域名，拒绝重定向。不得把不受信任的中转商当作可信供应商。

`api_key`只写，GET/PUT只给 `api_key_configured` 状态；空字符串/省略保留，`clear_api_key=true`清除当前引用（需先禁用或切到继承）。Key不写入SQLite、审计、模型上下文和前端存储；保存在DB目录下 `role-secrets`（或部署管理员的 `AI_OPS_ROLE_SECRET_DIR`）的0700目录、0600原子写入文件，文件名由角色ID哈希和随机修订号派生。并发请求各自保留不可变凭据修订，数据库提交失败删除新文件。切回继承保留原独立凭据；清除/换Key使旧修订不再用于新调用，已有调用与旧备份可能仍持有旧凭据，撤销凭据应在供应商侧轮换。旧修订目前不自动清理，避免破坏进行中的请求/备份；主机管理员按备份保留周期处理。

`max_model_steps:null`是真正不限制步骤次数；新角色默认null，已有显式6/8不迁移改变，省略更新也保留旧值。非null范围1–1000，0无效。输出和上下文预算仍真实有限，达到限额或接口截断即错误，不声称任务完成。不限步骤可能持续产生费用；取消、审批、账户授权仍有效。禁用模型停止后续调用，不代替取消已提交的远程命令。每日记忆归档也按该角色独立接口/模型路由，未启用时保留确定性归档，不额外付费调用。

`POST /api/v1/roles/{role_id}/messages`：

```json
{
  "text": "检查注册资产test-linux的执行身份并总结",
  "execution_users": ["ops_read"],
  "mode": "confirm",
  "idempotency_key": "caller-unique-message-id"
}
```

`mode` 有三种，且是**真实工具权限**而非仅提示词引导：

- `readonly`（只读分析）：模型**根本拿不到 `execute_command` 工具**，只能分析并使用只读搜索工具。即使提示词被绕过也无工具可调。
- `confirm`（询问确认后修改，默认）：模型提出的每条命令进入 `awaiting_approval`，需人工批准后才入队。
- `direct`（直接修改）：命令直接入队执行。

模式在入队时固定，模型不能修改、不能扩大账号、不能跳过确认。只读搜索工具（web_search/fetch_page）在所有模式下都可用（它们无法改动主机）。若只读轮次仍收到 `execute_command`（不应发生），服务会拒绝整轮而非静默忽略。

返回turn_id；`GET /api/v1/turns/{turn_id}`查询状态、当前子任务、最终回复、错误；`GET /api/v1/roles/{role_id}/turns`查看角色记录。空账号列表可以纯分析，但模型没有执行工具。

定制任务输入使用相同队列，trigger响应附带turn_id。角色启用且供应商配置正确后，可由模型处理；accepted/queued仍不代表已执行。事件状态随轮次更新。

## 工具权限边界

模型唯一的执行工具是execute_command，参数仅包括asset_id、run_as、command、timeout_seconds。禁止额外字段。execution_users、mode、role_id由主服务从本轮不可变快照取出；模型不能扩大账号列表或跳过确认。

工具下发前再检查实际账号在本轮名单中，且资产存在并配置该账号；执行Agent仍独立检查本地账号。目标账号的Linux权限是最终边界。已有root等高权限账号可以控制目标主机，这不是凭据绝对隔离承诺。

`confirm`模式下每条模型命令进入awaiting_approval，使用已有审批API批准；审批是控制通路，不排在角色后续消息队列。当前确认是逐条命令，不是无限授权整轮。

当前支持取消尚未执行的轮次/等待审批命令；运行中的模型请求或远程进程尚无完整取消通路。unknown执行阻塞整个角色，不能自动重跑或用模型总结掩盖。

## 上下文和文档

每次模型调用（包括工具结果后的下一次）注入：
1. 环境与工具边界说明。
2. **产品自述**（`SYSTEM_SELF_DESCRIPTION`）：模型所属产品是「AI Ops」，以及该产品的能力面（轮次/任务审批/资产/角色记忆/文档技能/定制任务/告警/渠道/控制台）和模型自己的真实工具边界。见 [docs/system-self-doc.md](system-self-doc.md)。
3. 服务当前全部OpenAPI接口文档全文（`FULL_SERVICE_API_DOCUMENTATION`）；接口是**产品的管理面**，不是模型的工具。
4. 所有核心文档全文，以及指定本角色的非核心文档全文。
5. 本轮角色、账号、确认模式及当前注册资产信息。
6. **同天更早轮次回放**（`recent_turn_messages`，本角色、更早seq、已完成、非command，上限`RECENT_TURNS`）：保证同一角色的连续对话不「失忆」。
7. 本角色的**按天分层记忆**（最近天全文、较旧压缩、更早梗概；非全文的旧日期列出日期提示可经记忆接口按需取全文），以及本轮完整模型/工具交互。记忆方案见 [docs/memory.md](memory.md)。

不同角色的私人对话不混入；历史授权快照不拿来当本轮权限。超出显式上下文预算时报错，不静默裁剪接口或核心文档。

文档API：`PUT /api/v1/documents/{id}`、`GET /api/v1/documents`、`DELETE /api/v1/documents/{id}`。字段id/name/content/core/role_ids；保存有revision，删除后不再注入，历史审计保留。文档自动写入trigger和并发编辑冲突检测尚未实现。

## 审计和异常

模型请求全文在调用前事务落盘；审计写入失败就不发送请求。正常回复/工具参数/usage记录在model_calls；查询 `/api/v1/turns/{id}/model-calls`即“本轮完整注入内容”的后端基础。认证头与Key不入记录；这不等于可以安全把秘密写进用户消息或文档。

网络失败不自动重复付费调用；轮次失败并保留错误类型。调用期间主服务崩溃，恢复后标MODEL_CALL_INTERRUPTED，不因响应未知而再次派发命令。完整模型调用计费核算、传输错误详情脱敏、人工恢复和任务可继续执行策略仍待完善。

## 迁移与测试

schema3新增轮次/角色模型/模型调用/文档表，给旧任务与trigger事件补turn_id，按旧记录创建时间归并入同一角色队列。原数据和审计不删除。升级需SQLite backup API备份；旧镜像不认识新schema，不能仅换旧镜像回滚。

自动测试覆盖多步骤不交叉、先到人工任务阻塞模型、账号扩权/模式伪造拒绝、不同角色并行、审批门控、未知执行阻塞、文档每次全文注入、角色历史隔离、上下文超限、模型调用前审计故障、重启中断和触发任务统一队列。
