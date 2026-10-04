# 三机极限集成测试 — 发现与处理

来源：2026-10-04 三机真机极限集成测试（.94 / .239 / .19，全部私网互通）。

测试结论：核心链路可靠（跨机 Agent、SSH 连接器、多主服务联邦、真实大模型轮次、租约/unknown/取消全部真机通过）。
下列问题集中在“跨机部署体验 / 权限与文档提示”，已逐项处理。

## 已修复

### 1. 跨机部署缺一键 https 配对码
- 问题：`install-control.sh` 生成配对码时写死 `http://127.0.0.1:<port>`，跨主机 Agent 无路可走。
- 修复：新增 `--public-url <https://origin>`，按 https origin 签发配对码；传非 https 直接拒绝。
- 覆盖：`deploy/README.md` 增加“一步生成跨机配对码”小节；脚本收尾提示说明用法。

### 2. SSH secret 权限导致静默 CONNECTION_FAILED
- 问题：secret 文件本身可读、父目录 700 root → 容器内 Permission denied → 笼统 `CONNECTION_FAILED`。
- 修复：`connector_ssh._load_secret` 区分 `SECRET_NOT_FOUND` / `SECRET_NOT_READABLE`；`_classify_connect_error` 把连接期失败细分为 `HOST_KEY_NOT_PINNED` / `HOST_KEY_INVALID` / `SECRET_*` / `CONNECTION_FAILED`；`ssh/check` 返回可读的 `error` 文本。
- 覆盖：`docs/connector-ssh.md` 增“secret 文件权限（常见坑）”一节与权限示例；新增 3 个单测。

### 3. 主服务挂 model_key 缺失会建成目录 → exit 127
- 问题：`docker` 对缺失路径的 bind-mount 会创建目录，容器随即启动失败。
- 修复：`compose.model.yaml` 与 `docs/model-turns.md` 明确“文件必须先存在”，给出创建命令。

### 4. unknown 阻塞角色的积压风险
- 处理：保留安全行为（不自动重跑），在 `docs/leases.md` 新增“积压风险与恢复建议”。

## 已确认设计正确（无需改动）

### 5. stale lease 易被误读
- 实际已在控制台“需人工处置”页签以“陈旧租约（仅观测，未重派）”卡片呈现并高亮，无需改动。

## 明确不改（有意取舍）

- Agent 拒绝非 loopback 明文 HTTP：安全默认，正确。
- 租约过期不自动改变任务状态、不自动重派：防止重复执行变更命令，正确。
- unknown 阻塞整条角色队列：安全优先，正确。
