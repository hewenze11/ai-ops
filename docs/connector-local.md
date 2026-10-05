# 本机 Connector（控制主机纳管）

## 定位

管理平台管不了自己，是个说不过去的缺口：控制服务所在的这台机器此前既没有
Agent、也没有 SSH Connector，因此**从不出现在资产列表里**。可一旦这台机器
自己的磁盘、CPU、内存出问题，平台就会在最关键的时刻一起失明。

本机 Connector 把**控制主机本身注册成一个普通资产**，接入方式为 `local`。

## 设计原则：复用，而不是另造一套

方案刻意**不新造权限与审计体系**，而是把本机当作"一个指向 localhost、免凭据、
默认存在的连接器"，与 Agent / SSH 资产走**同一套**机制：

| 能力 | 远端资产 | 本机资产 |
|---|---|---|
| 资产登记 | ✅ | ✅ |
| 执行通道 | Agent / SSH | **local（本模块）** |
| 权限白名单 | `allowed_users` | **`allowed_users`（同）** |
| 任务 / 租约 / 审计 | ✅ | **✅（同）** |
| 提交任务校验 | `run_as ∈ allowed_users` | **同** |

因此本机资产的"特殊"**只有两点**：① 不分发任何凭据；② 开机即存在。
除此之外全部同源——这正是"皮实耐用、不重复造轮子"的落脚点。

## 行为

- **默认注册**：控制服务启动时调用 `ensure_local_asset()`，若未注册则插入一条
  `connection_type='local'` 的资产，ID 固定为 `control-host-local`。
- **幂等**：重复启动不会重复注册；`ensure_local_asset()` 可安全多次调用。
- **无可用凭据**：本机资产写入一个不可用的 `token_hash`，因此
  `POST /api/v1/agents/{id}/claim` 永远无法以它认证，`rotate-token` 也会被拒
  （409）。它永远拿不到 `agent_token`。
- **执行**：任务照常入队、被 `LocalExecutor.claim()` 领取、开租约、执行、写
  结果、关租约、落审计。命令通过 `subprocess` 运行：
  - POSIX：当前用户即 `run_as` 时用 `/bin/sh -c`；否则 `sudo -n -u <run_as> -- /bin/sh -c`
    （`-n` 确保无 TTY 时不提示、缺权限即失败）。
  - Windows：无 `sudo` 与账号切换，经 `cmd.exe /d /s /c` 以服务账号运行。
- **超时 / 取消**：与 SSH Connector 同语义。POSIX 下进程放入独立进程组，
  超时或取消时对整组 `SIGKILL`，尽量回收逃逸的子进程。
- **输出上限**：`stdout`/`stderr` 各截断到 64 KiB，超出置
  `output_truncated=true`。
- **脱敏**：结果在落库与进入模型上下文之前经 `scrubbing.redact_result` 处理。

## 安全边界（本机比远端更该收紧）

本机是**控制端自己**：对它执行命令，等于在"大脑"里动刀。远端机器被改坏大不了
重装一台；控制端被改坏可能连远程恢复的机会都没有。

因此建议：

- 本机资产的 `allowed_users` **收窄到最小**（通过 `PUT /api/v1/assets/{id}/notes`
  调整）；
- 高风险操作优先使用 `confirm` 模式，而不是 `direct`；
- 不要把本机资产当作"方便的执行兜底"而放宽权限。

## 开关

- `AI_OPS_LOCAL_CONNECTOR_ENABLED=0`：启动时不注册本机资产（默认 `1`）。
- `AI_OPS_LOCAL_RUN_AS=<user>`：覆盖默认执行账号（默认：POSIX 下 root、其它平台
  当前用户）。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/local-connector` | 查询本机纳管状态（enabled / asset_id / allowed_users / run_as） |
| POST | `/api/v1/local-connector/disable` | 停用：隐藏本机资产（不删除历史） |
| POST | `/api/v1/local-connector/enable` | 启用 / 恢复本机纳管 |

### 为什么停用是"隐藏"而非"删除"

任务与轮次通过外键引用资产行。历史记录本身就是运维证据——**删除资产登记不等于
删除发生过的事实**。因此停用只翻转 `local_host.enabled`：资产从列表与 worker
的目标中消失，但每一条历史任务仍然可读。这与"注销（保留历史、可恢复）"的
产品方向一致。

## 控制台

"资产与知识 → 本机"页签展示纳管状态、允许账号与默认执行账号，提供启用/停用
按钮，并提示本机权限应比远端更严。

## 测试

`tests/test_connector_local.py`（12 项）覆盖：默认注册、幂等、无可认证凭据、
正常执行与审计、`run_as` 越权拒绝、失败退出码、取消、超时、大输出截断、
停用保历史、重新启用、环境变量关闭。
