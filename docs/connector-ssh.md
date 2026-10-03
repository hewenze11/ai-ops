# SSH Connector 设计（执行方式二选一）

资产注册时必须二选一：`agent`（本机执行 Agent）或 `ssh`（主服务直连执行）。同一资产只用一种方式，不做双路执行或自动切换。

## 与 Agent 的异同

| 维度 | Agent | SSH Connector |
| --- | --- | --- |
| 执行位置 | 目标机上的 Agent 进程 | 主服务进程内（paramiko 直连） |
| 领取模型 | Agent 主动 `claim` | 主服务连接器 worker 直接派发 |
| 凭据 | 资产 `agent_token` | SSH 主机/端口/账号/认证（密钥优先） |
| 在线判定 | 心跳（30 秒） | 连接可达性 |
| 输出/超时/取消 | 协议 1.1 | 主服务本地实现，语义对齐 |
| 任务状态机 | 与 Agent 相同 | 与 Agent 相同 |

两者共用同一套 `roles / role_turns / tasks / task_leases / audit`，因此模型轮次、租约、unknown 人工处置、审计的行为一致。

## 资产模型

`assets` 增加 `connection_type`（`agent` | `ssh`，默认 `agent`）。SSH 资产的连接信息放在独立表 `asset_connections`，不与 `task` 混合：

- `host`、`port`、`username`、`auth_kind`（`key` | `password`）。
- 主机公钥必须预置：`ssh_host_key` 存一行 OpenSSH 公钥（`ssh-ed25519 AAAA... comment`）。连接器把它作为**唯一**可信主机密钥，`RejectPolicy` 拒绝其他任何密钥；未预置则直接拒绝连接（fail closed），不做 TOFU。
- 私钥/密码**不落明文**：存 `secret_ref`（指向服务器上的密钥文件路径）或加密后的密文。凭证只在连接器进程内解密使用，绝不进入模型上下文、审计、日志或 GET 响应。公钥本身不是秘密，但 GET 响应只回 `ssh_host_key_pinned` 布尔值，不回密钥内容。

## 派发流程（SSH 资产）

1. 任务进入 `queued`（与 Agent 一致，`mode=direct`）。
2. 连接的 `role_turn.state='waiting_tool'` 且无更早未完成轮次时，连接器 worker 取任务。
3. 连接器开租约（`open_lease`）、置 `claimed`，通过 SSH 执行命令。
4. 执行使用：非交互 shell、`timeout`、有限输出、按 `run_as` 切换用户（`sudo -n -u`，无免密即失败，不换 root）。
5. 结果写回 `tasks.result`，关闭租约，置轮次状态，写审计——与 Agent 路径同一套校验。
6. 连接中断无法确认结果 → 上报 `unknown`，进入人工处置，绝不重放命令。

## 取消

取消语义对齐 Agent：`queued/awaiting_approval` 直接取消；`claimed` 置 `cancel_requested_at`，连接器轮询到后终止远程进程组，回 `cancelled`；`unknown` 不可取消。

## 安全边界（不宣称的）

- 不承诺任意 SSH 端点绝对安全；只做到密钥认证优先、预置主机公钥并严格校验（拒绝未预置密钥、拒绝中途密钥变更）、拒绝明文密码回显、输出有限。
- 不承诺连接器杜绝凭据泄露（用户已明确接受剩余风险）。
- 远端命令注入面与 Agent 一致：模型只能通过受校验的 `execute_command` 工具产出命令，命令本身按用户选定账号在其目标机执行。

## 实现顺序

1. 资产模型加 `connection_type` + `asset_connections` 表（迁移到 schema 6）。
2. SSH 连接器 worker（paramiko）：租约、执行、超时、输出、结果回写。
3. 取消通路。
4. 真机验证脚本：用测试机自身当 SSH 目标，验证 direct/confirm/取消/unknown/输出归档。
