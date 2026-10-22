# 权限体系落地计划（2026-10-22 起）

依据：`readonly-and-authorization.md` §39、§41–§48（以最新节为准）。原则：**加挂现有链路，不推倒重来**。现有骨架（模型工具调用 → 服务端 `turns.py` 校验 → 任务队列 → Agent `execute()` 按原生账号执行 → 结果脱敏入库）全部保留。

每个阶段独立可验收（单测 + 本地回归 + 真机 smoke），完成后在本文件勾选并写验证证据，再进入下一阶段。任何阶段中断，从本文件状态恢复。

## 阶段 0：现状盘点与接口冻结（半天）
- [ ] 跑通两仓全量测试，记录基线通过数。
- [ ] 冻结协议扩展字段：任务 `kind`（`shell`|`readonly`|`change`），旧任务无 `kind` 视为 `shell`，Agent 老版本遇到未知 `kind` 必须拒绝（fail closed）。
- 验收：基线测试全绿；协议文档 `protocol-v12.md` 草案。

## 阶段 1：闸1/闸2 只读通道（主服务 + Agent）
对应 §4、§39.2、§45、§48。
- [ ] 主服务：新增 `ai_ops/readonly_tools.py`，专用只读工具 `read_sanitized(path)`、`stream_sanitized(source)`、`list_dir`、`service_status`、`recent_logs` 等，模型只填结构化参数，不拼 argv。
- [ ] 协议：任务 `kind=readonly`，下发 `program + args` 结构化，禁止字符串命令。
- [ ] Agent：独立白名单复验 + `execve` 直执行（无 `sh -c`）；realpath 归一化；拒解释器/`less`/`curl`。
- [ ] 脱敏视图：Agent 读取最早一层脱敏（复用 `scrubbing.py` 规则 + 凭证明文精确匹配），回传前再过一次（双层）。
- [ ] `readonly` 模式的工具面 = 只读工具 + 现有问答/记忆工具；`execute_command` 继续不出现。
- 验收：单测覆盖白名单绕过用例（符号链接、`..`、`-fprintf`、解释器）；真机 smoke：读 `/etc/nginx/nginx.conf` 成功、读 `/etc/shadow` 被拒、含密钥文件回传已打码。

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
