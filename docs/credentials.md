# 凭据轮换

服务里有多类凭据，轮换方式各不相同。下面说明其存储、提交与轮换边界。

## 资产 Agent Token

每个 `agent` 资产在注册时一次性获得 token（数据库只存 `sha256`）。轮换接口：

```
POST /api/v1/assets/{id}/rotate-token     {"grace_seconds": 0}
```

- 返回**新的 agent token（仅此一次）**，绝不写入审计。
- `grace_seconds` 控制**旧 token 的重叠窗口**：
  - `0`（默认）：旧 token **立即失效**。
  - `>0`：旧 token 在窗口内仍可通过校验，便于灰度切换客户端配置；窗口到期后自动失效。
- 校验顺序：先比对当前哈希，不匹配再比对 `previous_token_hash`（且未过期）。两者都不中以 `403` 拒绝。
- 只对 `agent` 资产可用；对 `ssh` 资产返回 `409`（SSH 资产没有 agent token）。
- `GET /api/v1/assets/{id}` 返回 `previous_token_active` / `previous_token_expires`，便于确认窗口状态。

数据库层：`assets` 增加 `previous_token_hash`、`previous_token_expires`（schema 7，`ALTER TABLE` 迁移）。

**运营流程**：先在目标机写入新 token → 同一次轮换带上足够长的 `grace_seconds` → 观察旧 token 不再被使用 → 无需再次操作，窗口到期即失效。（若想立即收紧，可用 `grace_seconds=0` 直接切换，接受旧 token 立刻失效。）

## 管理凭据（admin token）

admin token 只存在于只读秘密文件，**从不入库**。运行中的服务在**每次校验时重读该文件**，所以替换文件即可轮换、无需重启：

```
ai-ops-admin-token --path /path/to/admin_token
```

- 原子写入新 token（先写 `.new`、fsync、再 rename），权限 0600。
- 旧文件保留为 `<name>.previous`（单一回退副本），便于回滚。
- **不打印 token 值**，只打印写入位置。
- 若文件缺失/不可读（如误删挂载），服务回退到启动时载入的 token，避免把运维者锁在门外。

## 定制任务 Trigger Token

`custom_tasks` 的 trigger token 在数据库只存哈希。**创建任务时返回一次明文；普通编辑保持原Token，不返回明文，也不自动轮换。**

管理员专用接口：

- `POST /api/v1/custom-tasks/{id}/trigger-token`：申请或替换当前唯一有效Token，新明文仅返回本次，旧Token立即失效。
- `DELETE /api/v1/custom-tasks/{id}/trigger-token`：立即撤销，任务详情仅显示 `trigger_token_active` 状态；恢复外部触发需重新申请。
- 外部调用仍是 `POST /api/v1/triggers/{id}/invoke`，使用**该任务专属** `Authorization: Bearer <TASK_TOKEN>`；管理Token不作为外部触发的替代凭据。撤销阻止旧Token的新请求，不撤回已受理轮次，也不关闭内部定时调度。

控制台新建成功后立即展示完整触发URL、一次性Token和接入说明，无需关闭重开；重复点击创建被阻止。已有任务编辑页提供同一面板，可申请、撤销以及复制URL/请求头/JSON/Bash/PowerShell示例。示例仅含 `<TASK_TOKEN>` 占位符，不自动嵌入真实Token、不自动发请求。实际执行示例会创建真实任务，不是dry-run。

完整URL按当前浏览器入口生成，调用方须能访问该地址；私网入口不会自动变为公网。推荐HTTPS，公网HTTP不能保护Token传输。`X-Event-ID` 用于同任务同正文重试去重，新事件换新ID；不传每次新建。HTTP202只表示入队，不表示执行成功。

一次性Token只在本次弹窗显示，关闭/退出后不再读回；重新打开只显示状态。复制Token会将其放入系统剪贴板，关闭弹窗不清除系统剪贴板。申请/撤销中清除旧明文，失败提示“状态未确认”，需重新打开核实，不把未知结果说成成功。

调用示例（只提供格式，不会自动执行；替换入口、任务ID及Token）：

Bash / POSIX：

```bash
curl --fail-with-body -X POST 'https://console.example/api/v1/triggers/TASK_ID/invoke' -H 'Authorization: Bearer <TASK_TOKEN>' -H 'Content-Type: application/json' -H 'X-Event-ID: alert-unique-001' --data '{"message":"检查告警","labels":{"service":"demo"}}'
```

PowerShell（避免Windows的curl别名和中文编码差异）：

```powershell
$headers = @{ Authorization = 'Bearer <TASK_TOKEN>'; 'X-Event-ID' = 'alert-unique-001' }
$body = '{"message":"检查告警","labels":{"service":"demo"}}'
$bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
Invoke-RestMethod -Method Post -Uri 'https://console.example/api/v1/triggers/TASK_ID/invoke' -Headers $headers -ContentType 'application/json; charset=utf-8' -Body $bytes
```

401表示缺Bearer，403表示无效/撤销/跨任务Token，409可能是停用/已删除/幂等正文冲突/旧权限需重选，413为请求过大（上限256KiB），422为JSON或参数不合法。鉴权在去重前执行，旧Token不能靠重复事件ID绕过撤销。一个URL只触发一个任务，不广播；Alertmanager按receiver路由，原生alerts数组不自动拆分，groupKey也不自动充当幂等键。

## SSH 密码 / 私钥（2026-10-10）

资产页 → 注册资产 → SSH，或已有SSH资产 → 连接设置：

- 默认直接输入密码或完整私钥；密码遮挡显示，私钥用多行文本框。当前执行器只支持**无口令 Ed25519 私钥**，不支持私钥解锁口令或其他算法；保存成功不代表可登录，请另点“测试已保存 SSH 连接”。
- 编辑时默认“保留已有凭据”，不会读回旧密码、私钥或文件路径。选择输入新凭据才能替换；更换认证方式时必须同时提供对应凭据。仅修改地址、端口、登录账号不会清除原凭据。
- 高级“服务端文件路径”兼容管理员预置的外部凭据文件；它不自动加密或搬迁已有明文文件。
- 密码的前后空格、私钥的换行原样提交；切换凭据来源/认证方式或切回Agent会清空当前输入。提交返回后（成功或失败）清空凭据框；失败后需要重新输入。关闭弹窗或锁定会清除表单DOM，不写入浏览器本地存储；这不等于对浏览器进程内存或扩展作安全擦除保证。
- 输入正文经 `ssh_secret` 写入；核心注册接口、控制台注册别名和连接修改接口均按相同方式加密落盘，数据库只存引用，响应与审计不回显正文。凭据不是资产备注，不注入模型上下文。
- 必须通过HTTPS保护传输；加密落盘不能补救公网HTTP泄漏。主机公钥须从可信渠道核实并固定，编辑留空保留，不自动信任新主机。
- 托管凭据替换不会删除旧修订，也不修改远端密码或 `authorized_keys`；管理员须先规划两端切换。备份必须保留密文和密钥旁车，详见 `backup.md`。

## 不宣称的

- 不宣称凭据绝对安全：持有 token 的机器被攻破后仍可能泄露风险（用户已明确接受该剩余风险）。
- 不做自动过期、不做轮换提醒、不做密钥托管/KMS。
- admin token 的轮换依赖文件挂载可写；在 `read_only` 容器里应从宿主机替换挂载文件，而不是在容器内运行 CLI。
