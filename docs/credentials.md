# 凭据轮换

服务里有三类凭据，轮换方式各不相同，但都不把明文写进数据库、审计或日志。

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

`custom_tasks` 的 trigger token 与资产 token 同构（库中只存哈希），注册/编辑时一次性返回。当前没有独立轮换接口——需要更换时重新编辑该定制任务即可获得新 token。这是已知的简化。

## 不宣称的

- 不宣称凭据绝对安全：持有 token 的机器被攻破后仍可能泄露风险（用户已明确接受该剩余风险）。
- 不做自动过期、不做轮换提醒、不做密钥托管/KMS。
- admin token 的轮换依赖文件挂载可写；在 `read_only` 容器里应从宿主机替换挂载文件，而不是在容器内运行 CLI。
