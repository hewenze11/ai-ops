# 权限体系落地计划（2026-10-22 起）

依据：`readonly-and-authorization.md` §39、§41–§48（以最新节为准）。原则：**加挂现有链路，不推倒重来**。现有骨架（模型工具调用 → 服务端 `turns.py` 校验 → 任务队列 → Agent `execute()` 按原生账号执行 → 结果脱敏入库）全部保留。

每个阶段独立可验收（单测 + 本地回归 + 真机 smoke），完成后在本文件勾选并写验证证据，再进入下一阶段。任何阶段中断，从本文件状态恢复。

## 阶段 0：现状盘点与接口冻结（半天）
- [x] 跑通两仓全量测试，记录基线通过数。
- [x] 冻结协议扩展字段：任务 `kind`（`shell`|`readonly`|`change`），旧任务无 `kind` 视为 `shell`，Agent 老版本遇到未知 `kind` 必须拒绝（fail closed）。
- 验收：基线测试全绿；协议文档 `protocol-v13.md` 草案（1.2 已被联邦占用）。

## 阶段 1：闸1/闸2 只读通道（主服务 + Agent）
对应 §4、§39.2、§45、§48。
- [x] 主服务：新增 `ai_ops/readonly_tools.py`，专用只读工具 `read_file`、`list_dir`、`file_stat`、`disk_usage`、`memory_info`、`process_list`、`service_status`、`journal_logs`、`k8s_get`、`k8s_logs`，模型只填结构化参数，不拼 argv。
- [x] 协议：任务 `kind=readonly`，下发 `program + argv` 结构化，禁止字符串命令。
- [x] Agent：独立白名单复验 + `execve` 直执行（无 `sh -c`）；程序必须是白名单绝对路径；拒解释器类；拒输出重定向参数。
- [ ] realpath 归一化：已实现（`readonly._real`，符号链接即拒），**未真机验证**。
- [x] 脱敏视图（第一版）：Agent 收到 readonly 输出后在本机按同一套规则打码再上行；服务端入库前仍有一层。
- [x] `readonly` 模式的工具面 = 只读工具 + 现有问答/记忆工具；`execute_command` 继续不出现。
- 验收：单测已覆盖绕过用例（非白名单程序、相对路径、换行注入、输出重定向参数、shell 元字符）；**真机 smoke 未做**：需在 .94 上执行“读 nginx.conf 成功 / 读 shadow 被拒 / 密钥文件已打码”。

## 阶段 2：闸3 变更集 `propose_change`（主服务）
对应 §5、§39.1、§43.2、§43.5。
- [ ] 动作表 `ai_ops/actions.py`：有限结构化动作（restart_service / edit_file_patch / k8s_patch / run_shell[仅人工] …），每条带 `auto_approvable`、主机范围、参数校验。
- [ ] 新工具 `propose_change(变更集)` 替代 `execute_command` 的裸 shell（`execute_command` 降级为 `run_shell` 动作，只能走人工审批）。
- [ ] 策略引擎 `ai_ops/policy.py`：输入（模式, 动作, 触发源, 主机）→ 直接执行 / 发审批 / 拒绝。`direct` 禁止用于告警/webhook 触发轮次。
- [ ] 审批绑定载荷哈希（§39.3），执行前重校验。
- [ ] Agent：`kind=change` 任务按动作模板展开为 argv 执行，`cred:xxx` 引用在 Agent 侧替换（§42）。
- 验收：confirm 模式动作进 awaiting_approval 且哈希一致才入队；direct 模式仅 auto_approvable 直过；触发源收窄生效。

## 阶段 3：模式固化与名单快照（主服务）
对应 §43.1、§44、§45。
- [ ] 任务启动固化模式，切模式=新任务；trigger 必填模式；放权不继承。
- [ ] 名单不可变快照：被引用即冻结，编辑 fork 新 ID 带 `parent_id`；禁止优先于允许；机械检查（死规则/相交/回溯）。
- 验收：现有 `test_modes.py` 扩展；快照 fork 与绑定 diff 提示测试。

## 阶段 4：凭证两档与 Agent 侧凭证存储
对应 §42、§46、§47。
- [ ] 密钥发现即凭证化：脱敏命中生成凭证句柄，卡片询问是否解开（§42.2）。
- [ ] Agent 侧 `credentials.json`（600）、`aiops-agent cred list|share|revoke|destroy`、`register --mode own|cooperative`。
- [ ] 主服务只存句柄；connector 模式降级路径存主服务（复用 `connector_secrets.py`）。
- 验收：对话永不出现明文；凭证页为唯一展示面；own 模式自动上行测试。

## 阶段 5：兜底与安装
对应 §7、§10、§43.4。
- [ ] `install.sh`：建只读账号 + sudoers 仅放具体命令；核心文件 600 属服务账号；审计表独立 DB 角色仅 INSERT/SELECT。
- [ ] 外联出口说明与默认关闭自由 fetch（§41）。
- 验收：.94 真机从零安装，验收清单跑通。

## 阶段 6：控制台对接（Opus 5.5 子代理，主助手只给接口）
- [ ] 审批卡片（动作主体、diff、哈希）、模式选择、名单编辑/快照 diff、凭证页。
- 验收：用户亲自审。

## 进度记录
（每阶段完成时追加：日期 / 改动文件 / 测试结果 / 真机证据 / 遗留）

- 2026-10-22 阶段 0 完成。基线：ai-ops 488 passed（3m50s）；ai-ops-agent 30 passed / 13 skipped（Linux-only 用例，Windows 跳过）。新增 `docs/protocol-v13.md`。本地仓与 GitHub 对齐（两仓此前落后远端 install.sh/CI 提交，已取回；本地未提交的 docs 已推送）。遗留：Agent Linux 用例须在 .94 真机跑。
- 2026-10-22 阶段 1 代码完成、单测全绿，**真机 smoke 未做**。主服务 508 passed（+20）；Agent 42 passed / 14 skipped。改动：新增 `ai_ops/readonly_tools.py`（主服务侧封闭只读工具集）、`ai_ops_agent/readonly.py`（Agent 侧独立复验 + execve 无 shell + 本机脱敏），`turns.py` 接上 readonly 工具分支，`authority.py` 允许 readonly 轮次的 claim，`execution.py`/`app.py` 支持协议 1.3 且不向旧 Agent 下发新 kind（审计去重）。遗留：①.94 真机 smoke（本机无 SSH 密钥，无法执行）；②`read_stream`/日志流式脱敏未做，当前只拉尾部 N 行；③`cred:` 字面量注入属阶段 4。
