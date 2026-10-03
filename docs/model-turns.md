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
export AI_OPS_MODEL_BASE_URL=https://your-provider.example/v1
export AI_OPS_MODEL=your-verified-model-id
docker compose -f compose.yaml -f compose.model.yaml -p ai-ops-preview up -d
```

不配置Key时不启动模型工作线程。配置全局Key也不自动启用所有角色；每个角色模型设置默认enabled=false，防止升级后把旧测试队列一口气发往收费模型。

## 角色模型与消息API

`PUT /api/v1/roles/{role_id}/model`（管理员）：

```json
{
  "enabled": true,
  "model": "",
  "max_model_steps": 8,
  "max_output_tokens": 2048,
  "max_context_chars": 250000
}
```

model为空继承全局配置；非空覆盖模型名称，目前仍共用一个全局供应商地址和Key。角色多供应商/多凭据配置尚未实现。步骤、输出和上下文预算明确可配；耗尽就报告错误，不声称任务已完成，也不限制等待队列长度。禁用角色模型停止后续模型调用，不代替取消已提交的远程命令。

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
2. 服务当前全部OpenAPI接口文档全文。
3. 所有核心文档全文，以及指定本角色的非核心文档全文。
4. 本轮角色、账号、确认模式及当前注册资产信息。
5. 本角色最近10个已完成非命令轮次的对话摘要面（原用户消息和最终回复），以及本轮完整模型/工具交互。

第5项是临时同角色最近对话实现，不是已完成的按天全文/压缩/梗概记忆方案。不同角色的私人对话不混入；历史授权快照不拿来当本轮权限。超出显式上下文预算时报错，不静默裁剪接口或核心文档。

文档API：`PUT /api/v1/documents/{id}`、`GET /api/v1/documents`、`DELETE /api/v1/documents/{id}`。字段id/name/content/core/role_ids；保存有revision，删除后不再注入，历史审计保留。文档自动写入trigger和并发编辑冲突检测尚未实现。

## 审计和异常

模型请求全文在调用前事务落盘；审计写入失败就不发送请求。正常回复/工具参数/usage记录在model_calls；查询 `/api/v1/turns/{id}/model-calls`即“本轮完整注入内容”的后端基础。认证头与Key不入记录；这不等于可以安全把秘密写进用户消息或文档。

网络失败不自动重复付费调用；轮次失败并保留错误类型。调用期间主服务崩溃，恢复后标MODEL_CALL_INTERRUPTED，不因响应未知而再次派发命令。完整模型调用计费核算、传输错误详情脱敏、人工恢复和任务可继续执行策略仍待完善。

## 迁移与测试

schema3新增轮次/角色模型/模型调用/文档表，给旧任务与trigger事件补turn_id，按旧记录创建时间归并入同一角色队列。原数据和审计不删除。升级需SQLite backup API备份；旧镜像不认识新schema，不能仅换旧镜像回滚。

自动测试覆盖多步骤不交叉、先到人工任务阻塞模型、账号扩权/模式伪造拒绝、不同角色并行、审批门控、未知执行阻塞、文档每次全文注入、角色历史隔离、上下文超限、模型调用前审计故障、重启中断和触发任务统一队列。
