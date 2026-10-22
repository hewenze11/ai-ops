# 执行协议 1.3（草案，2026-10-22）

在 1.1/1.2 基础上新增任务**种类**字段，为只读通道与结构化变更集铺路。设计依据：`readonly-and-authorization.md` §39.2、§43。

## 任务字段 `kind`

claim 返回的 `task` 对象新增 `kind`，取值：

| kind | 含义 | 执行方式（Agent 侧） |
| --- | --- | --- |
| `shell` | 1.0–1.2 的裸命令任务（`command` 字符串） | 现状不变：`sh -c`，按原生账号。仅能来自人工审批通路 |
| `readonly` | 只读通道任务：`program` + `args` 结构化 | Agent 独立白名单复验后 **`execve` 直执行，禁止 shell**；输出先过本地脱敏视图再上行 |
| `change` | 变更集任务：`action` + `params` | Agent 按内置动作模板展开为 argv 执行；`cred:<id>` 引用在 Agent 侧替换为本地凭证 |

- **缺省兼容**：老主服务不下发 `kind`，Agent 视为 `shell`。
- **fail closed**：Agent 遇到未知 `kind`、或 `readonly`/`change` 校验失败，返回 `failed` 并带错误码 `UNSUPPORTED_KIND` / `READONLY_REJECTED` / `ACTION_REJECTED`，**不得退回 shell 执行**。
- 老 Agent（≤1.2）无 `kind` 概念：主服务按 `claimed_protocol` 判断，`readonly`/`change` 任务**不下发给 1.2 及以下 Agent**（claim 时跳过，任务留队并在控制台提示升级）。

## 只读任务载荷（`kind=readonly`）

```json
{"kind":"readonly","tool":"read_file","program":"/usr/bin/cat","args":["--","/etc/nginx/nginx.conf"],
 "run_as":"aiops-ro","timeout_seconds":30,"sanitize":{"literals_ref":["cred:3","cred:7"]}}
```

- `tool` 为主服务只读工具名（审计用），`program` 必须是绝对路径且 ∈ Agent 白名单；`args` 由主服务生成，Agent 再按同一规则校验（路径 realpath 归一化、禁 `-fprintf` 等参数）。
- `sanitize.literals_ref` 是 Agent 本地凭证句柄，Agent 取明文做精确匹配打码；主服务不下发明文。

## 变更任务载荷（`kind=change`）

```json
{"kind":"change","action":"systemd.restart","params":{"unit":"nginx"},"run_as":"aiops-ops",
 "approval":{"payload_sha256":"…"},"timeout_seconds":120}
```

- `approval.payload_sha256` = 审批时绑定的载荷哈希（§39.3），Agent 在执行前重算并比对，不一致拒绝。
- 动作表由主服务 `ai_ops/actions.py` 与 Agent 内置模板**各自持有同一版本**；版本不匹配的动作 Agent 拒绝。

## 版本协商

- Agent `claim` 的 `protocol_version` 升为 `1.3`；主服务接受 `1.0–1.3`。
- 心跳 `protocol_version` 同步升级；主服务据此决定是否下发 `readonly`/`change`。

## 状态

- [x] 字段与兼容规则冻结（阶段 0）
- [ ] 主服务下发 `readonly`（阶段 1）
- [ ] Agent 执行 `readonly`（阶段 1）
- [ ] `change`（阶段 2）
