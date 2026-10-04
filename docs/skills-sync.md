# Skills 订阅：从官网拉取 Skills

官网（`ai-ops-site`）是 Skills 分发中心；主服务（`ai-ops`）是消费端。订阅用户在官网付费后，
官网为他的账号发放一个 **pull key**（凭证，前缀 `aiops-sk-`）；主服务用这个 key 远程拉取
他有权限的 Skills 并导入本地 Skills 表，供角色上下文注入。

```
用户付费订阅 ──> 官网生成 pull key ──> 主服务用 key 拉取 ──> 导入本地 skills 表
                (/api/v1/me/pull-keys)   (POST /api/v1/skills/sync)
```

## 1. 官网侧：拿 key

1. 注册并登录官网。
2. 在「我的 / 会员」处购买 Skills 包月（模拟支付会立即解锁）。
3. 订阅生效后创建 pull key。**明文只在创建时返回一次**，请立刻保存到主服务机器上：

```bash
install -m 600 /dev/stdin /etc/ai-ops/skills.key <<'EOF'
<把你的 pull key 粘贴到这一行>
EOF
```

> key 只存哈希，官网也无法再显示明文。丢了就吊销旧的、重新生成一把。

## 2. 主服务侧：拉取

主服务提供受管理员保护的接口 `POST /api/v1/skills/sync`，以及命令行脚本
`scripts/skills_sync.py`（走同一接口，不需要数据库访问）。

```bash
# 拉取并绑定到本地角色 ops
python scripts/skills_sync.py \
    --control http://127.0.0.1:8765 \
    --admin-token-file /etc/ai-ops/admin.token \
    --hub https://your-site.example/api/v1/skills/repo \
    --pull-key-file /etc/ai-ops/skills.key \
    --role ops
```

`--dry-run` 只报告不写入，适合先看会导入什么。

也可以直接调接口：

```bash
curl -sS -X POST http://127.0.0.1:8765/api/v1/skills/sync \
  -H "Authorization: Bearer $AI_OPS_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"source_url":"https://your-site.example/api/v1/skills/repo",
       "pull_key":"aiops-sk-...","role_ids":["ops"]}'
```

## 3. 安全边界（务必读完）

- **pull key 只用于对官网的出站鉴权**，绝不出现在 URL、日志或审计记录中；脚本从文件/环境变量
  读取，不走命令行参数，避免泄漏到进程列表。
- **官网是"能拉哪些 Skill"的唯一权威**：它校验 key 与订阅状态。主服务只决定"拉到本地后绑给哪个角色"。
- **拉入的 Skill 不会扩张任何权限**。Skill 是注入的参考文本，不是代码：它不能新增账号、改执行模式、
  授予工具。即使官网被攻破返回恶意内容，最坏结果也只是"某个已有角色多了一段无效参考文本"。
- **不存在的角色会被跳过，绝不自动创建**。官网若把 Skill 绑到本地没有的角色，主服务会把它列入
  `skipped` 并丢弃该绑定，而不是凭空建角色。
- **订阅到期即失效**：官网对过期订阅返回 `402`，拉取直接失败，不会继续下发。
- **大小与超时上限**：单次响应上限 4 MiB、超时 20 秒，防止异常响应拖垮主服务。

## 4. 常见错误

| 现象 | 含义 | 处理 |
|---|---|---|
| `403 pull key is invalid or revoked` | key 错或已吊销 | 在官网重新生成一把 |
| `402 Skills subscription is expired` | 订阅过期 | 续费后重试 |
| `502 could not reach the skill hub` | 网络/地址不通 | 检查 `--hub` 地址与出网策略 |
| 导入结果里 `skipped` 有项 | 角色不存在或无绑定角色 | 用 `--role` 指定本地角色，或先建角色 |
