# 内置告警日志

更新：2026-10-03。用户需求（2026-10-02）：所有告警**默认写入内置告警日志**；
多告警源按定制任务独立 trigger 路径分发，**同一配置可供多个源共用**。

## 设计

- 告警日志 `alarm_log` 是**追加式持久表**，在**与触发决策同一个事务**里写入。
  它与模型的记忆无关，**无论后续角色轮次成功、失败还是被取消，都能独立存在**。
- **每条告警默认都落日志**，包括：
  - `accepted`：已被接受并生成角色输入；
  - `duplicate`：同一 `X-Event-ID` 重复投递（仍记录，不静默丢弃）；
  - `rejected`：配置被停用/删除时被拒绝（仍记录）。
- **多源共用一条配置**：一个定制任务就是一条 trigger 路径；不同告警源通过可选
  请求头 `X-Alarm-Source`（或 payload 里的 `host/monitor/source` 字段）标注来源。
  日志按 `source` 可分别查询，互不混淆。

## 来源与字段提取

`X-Alarm-Source` 是**不可信元数据**，只用于日志标注，**绝不作为指令或授权**。
若未提供，则依次取 payload 的 `source`/`host`/`monitor`，否则记为 `unknown`。
日志会尽力提取常见字段：`severity`（level/priority）、`title`（name/alert/subject）、
`summary`（message/description），提取不到则用整个 payload 作为摘要。

## 脱敏

写入前用与任务结果、审计详情相同的 `scrubbing` 规则**递归脱敏**：既按内容匹配
常见密钥形态，也**按字段名**（password/secret/token/api_key…）遮蔽，因此
`{"password": "..."}` 这类嵌套 payload 也会被遮蔽。原始 payload 全文仍保留（已脱敏）。

## API（均需管理员）

- `GET /api/v1/alarms?custom_task_id=&source=&after=&limit=`：按任务/来源/游标分页
- `GET /api/v1/alarms/sources?custom_task_id=`：各来源计数与最近序号
- `GET /api/v1/alarms/{alarm_id}`：单条明细（含脱敏后的 payload）

## 与审计、事件队列的区别

- `audit`：**全量操作审计**（谁在何时做了什么），不可编辑。
- `trigger_events`：**角色输入队列**（可去重、驱动角色轮次）。
- `alarm_log`：**面向告警的默认可读日志**，把"发生过什么告警"独立沉淀，便于按源
  排查，不依赖模型是否处理成功。

三者都落盘，语义不同，不互相替代。

## 测试

`tests/test_alarms.py`（9 项）：字段提取与回退、接受即落日志、多源共用并可按源分离、
`X-Alarm-Source` 覆盖 payload 且校验、重复投递仍记录、拒绝也记录、payload 脱敏、
告警不随轮次失败消失且可按任务查询。

## 后续（未实现）

- 告警日志的**保留/裁剪策略**（当前无限追加）。
- 告警→通知的外部推送（本期只做日志，不做推送）。
- 按 payload 字段的**路由规则**（用户已注明属可选后续能力）。
