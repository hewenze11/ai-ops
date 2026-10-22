# 心跳发送序号与接收证据（2026-10-08，本地实现）

## 口径

不是每10秒固定发送，也不是网络丢包率。Agent空闲循环约3秒、执行期间约2秒，加上网络与其他工作延迟；本功能不改变周期。对每个 `(asset_id, session_id, controller)`，取服务端已收到的最高发送尝试序号 H，窗口为 `[max(1,H-9), H]`，因此刚启动不足十次时不凑满十次。

- `received`：该序号的心跳确实通过资产凭据认证、通过校验并由本服务记录。
- `not_received_yet`：当前保留证据中没有该序号的接收记录；不是永久失败判定，迟到会更正。尤其不能把它解释为网络包永久丢失。
- `unknown_expired`：服务端已知该位置的证据超出24小时或容量边界，不计作已知未收到。
- `ack_status=acknowledged`：Agent后续回报它曾拿到 `accepted:true`；仍是发送端回报，不是服务端对响应送达的独立观测。
- `ack_status=unconfirmed`：Agent没有拿到有效成功ACK，包括连接失败、超时、HTTP错误、响应丢失或非accepted响应。**已收到 + unconfirmed 完全可能且会同时展示。**
- `ack_status=unknown`：尚无发送端回报，包括当前最新请求。不能由服务端自行假定ACK已抵达。

失联后不移动H、不生成尾部失败：展示“最后可观测N次”及 `updated_at`，30秒无该流新接收证据时 `stale=true`。未知尾部始终 `tail_attempts=unknown`，即便在线也不能保证没有更晚的在途尝试。24小时无证据后精确摘要消失，不补造历史；旧版观察窗继续存在到各自保留期届满。整个流的历史已过期或数据库刚建时，也不能证明此前这些序号在别处/过期记录中的接收情况；“未收到”严格指当前留存证据，而非全历史断言。

## 可选请求扩展

`POST /api/v1/agents/{asset_id}/heartbeat` 保留协议1.2及原字段，新增可省略/为null的 `heartbeat_telemetry`。旧Agent不需要改造才能继续接入。

```json
{
  "instance_id": "00000000-0000-4000-8000-000000000001",
  "agent_version": "0.1.0.dev3",
  "protocol_version": "1.2",
  "busy": false,
  "heartbeat_telemetry": {
    "session_id": "00000000-0000-4000-8000-000000000001",
    "controller": "team-a",
    "attempt_seq": 3,
    "previous_results": [
      {"attempt_seq": 1, "outcome": "acknowledged"},
      {"attempt_seq": 2, "outcome": "unconfirmed"}
    ]
  }
}
```

- `session_id` 是Worker启动时新建的小写UUID，与外层instance_id严格一致。重启从新会话的序号1开始，不写磁盘计数器。
- `controller` 是本机controller配置中的name（1–64字符，无控制字符），不是URL/Token。旧单主配置为default。身份与序号仅是通过资产Token认证的Agent声明，不是新增权限凭证，也不能证明Agent没有伪造序号。
- `attempt_seq` 必须是严格整数（拒绝bool/字符串/小数），范围1–9007199254740991；每次真正发起heartbeat HTTP请求前在短锁内递增，失败也占号；不同controller独立，网络I/O不持有该锁。
- `previous_results` 可省略，最多10项，序号互异且只允许当前序号之前十个位置。没有完成或已移出本地窗口的结果不回报，保持unknown；不伪造超时原因。
- 重复接收幂等；乱序可补接收或ACK证据，但不会降低H。已知旧会话的晚到包不切换当前会话；每个controller按服务端首次见到会话的顺序选最近会话，不按随机UUID排序。此前从未见过的会话无法仅靠UUID证明真实启动先后。
- 旧序号/已知旧会话仅补证据，不回滚当前busy/presence；原claim控制器身份和队列等待灯契约不变。

旧严格服务端可能422拒绝新增字段。新Agent只在错误精确为单个 `extra_forbidden`、位置为 `body.heartbeat_telemetry` 时，按controller停用扩展并立刻重发旧payload；该重发也独立计一次本地尝试。绝不在401/超时/非法嵌套字段等错误上降级。此降级持续到Agent重启，服务端升级后重启Agent即可重新启用；优先升级主服务再升级Agent。

## 读取接口与兼容

`GET /api/v1/assets` 的 `health.heartbeat` 与 `GET /api/v1/agents/{id}/status` 同源。

新摘要：
- `metric=heartbeat_attempt_receipt`，`actual_attempts=last_observable_sequence_window`，`window_size=10`。
- `streams[]`：各controller最近会话的独立摘要，绝不跨controller混算。每项包括 `controller/session_id/from_seq/through_seq/attempt_count/received_count/not_received_count/unknown_count/acknowledged_count/unconfirmed_count/updated_at/stale/attempts[]`。
- `attempts[]` 每项包含 `attempt_seq/receipt/received_at/ack_status`。
- 同名摘要字段在顶层也提供一份（服务端最近首次见到的流），多controller客户端应使用streams。
- 原 `observed_slots/successful_slots/success_ratio/missed_ratio/observation_window_seconds` **仍然表示旧10秒观察窗**，没有改成发送分母，供旧客户端继续使用。新客户端必须使用上面的attempt字段，不能把旧比率与新分母混算。
- 未收到新扩展、旧Agent重启或精确证据过期时，仍用 `metric=heartbeat_window_arrival`、`actual_attempts=unavailable`；UI明确“旧版降级/观察窗”。

`GET /api/v1/assets/{id}/heartbeats?after=0&limit=100` 默认 `view=windows`，原items字段与游标不变。

新增 `view=attempts`：
- `items[]`：`seq`（日志记录ID，**不是发送序号**）、`asset_id/session_id/controller/attempt_seq/received_at/ack_status/recorded_at`。仅本地ACK报告而没有接收证据时received_at为null。
- 同一个尝试后续收到迟到包/ACK报告时原记录原位更新，不另增重复条目。该API是快照分页，不是变更订阅；游标不会重新输出已读记录的更正，刷新第一页/读取summary可看到最新证据。
- `summary`：实时精确摘要（无数据时null），包含未接收序号的位置；因此无需为每个巨大缺号写数据库行。
- `next_after` 仍按当前view的日志ID分页。两个view的游标不可混用；limit最大500，after为非负安全整数。
- 全部读取接口沿用管理员认证。controller在展示/日志响应中经既有脱敏器处理；不发送controller URL/Token。

## 容量、安全与保留

新增两张幂等创建的表 `heartbeat_streams/heartbeat_attempts`；不改变既有schema user_version，不强制数据迁移。资产purge按外键顺序清理两表；原输出保留任务同时清理心跳证据，收到心跳/查询日志也会清理24小时之外的数据。只删除遥测，不删除审计证据。

每资产24小时内最多32个session/controller流；超限的新流返回429，已有流仍可接收。每流仅允许保留最近50000个序号位置的稀疏证据（正常约2秒心跳的24小时约43200次）；极端高频/大跳号会提前裁掉更老证据。窗口计算始终最多迭代10次，previous_results最多10项，不遍历缺号跨度，不因伪造巨大正数分配巨量内存。非法数据返回422且不更新presence；控制器身份不是权限边界，持有合法资产Token的恶意Agent仍能谎报其自有遥测。

不新增任意命令策略，不访问真实Agent、模型或Webhook。接收成功表示本API记录成功，不等于命令成功、网络绝对可靠、数据库异地备份成功或OS安全隔离。
