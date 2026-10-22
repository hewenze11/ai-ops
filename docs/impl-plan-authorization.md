# 权限体系落地计划（2026-10-22 起）

依据：`readonly-and-authorization.md` §39、§41–§48（以最新节为准）。原则：**加挂现有链路，不推倒重来**。现有骨架（模型工具调用 → 服务端 `turns.py` 校验 → 任务队列 → Agent `execute()` 按原生账号执行 → 结果脱敏入库）全部保留。

每个阶段独立可验收（单测 + 本地回归 + 真机 smoke），完成后在本文件勾选并写验证证据，再进入下一阶段。任何阶段中断，从本文件状态恢复。

## 阶段 0：现状盘点与接口冻结（半天）
- [x] 跑通两仓全量测试，记录基线通过数。
- [x] 冻结协议扩展字段：任务 `kind`（`shell`|`readonly`|`change`），旧任务无 `kind` 视为 `shell`，Agent 老版本遇到未知 `kind` 必须拒绝（fail closed）。
- 验收：基线测试全绿；协议文档 `protocol-v13.md` 草案（1.2 已被联邦占用）。

## 阶段 1：闸1/闸2 只读通道（主服务 + Agent）
对应 §4、§39.2、§45、§48。**已完成并在真机（172.236.254.94, Ubuntu）验收 11/11。**
- [x] 主服务：新增 `ai_ops/readonly_tools.py`，专用只读工具 `read_file`、`list_dir`、`file_stat`、`disk_usage`、`memory_info`、`process_list`、`service_status`、`journal_logs`、`k8s_get`、`k8s_logs`，模型只填结构化参数，不拼 argv。
- [x] 协议：任务 `kind=readonly`，下发 `program + argv` 结构化，禁止字符串命令。
- [x] Agent：独立白名单复验 + `execve` 直执行（无 `sh -c`）；程序必须是白名单绝对路径；拒解释器类；拒输出重定向参数。
- [x] realpath 归一化：`readonly._real`，符号链接即拒；真机已验证拒绝。
- [x] 脱敏视图（第一版）：Agent 收到 readonly 输出后在本机按同一套规则打码再上行；服务端入库前仍有一层。
- [x] `readonly` 模式的工具面 = 只读工具 + 现有问答/记忆工具；`execute_command` 继续不出现。
- 验收：单测已覆盖绕过用例；**真机 smoke 已完成**（`scripts/smoke_readonly_host.py`，11/11）：读 hostname 成功且带行号、`/bin/cat` 冒充被拒、`;id` 只是文件名、`-fprintf` 被拒、符号链接被拒、密钥样式与本地凭据字面量都打码、`/etc/shadow` 被 OS 权限拒绝、未授权账号拒绝、journal 可读。

## 阶段 2：闸3 变更集 `propose_change`（主服务）
对应 §5、§39.1、§43.2、§43.5。**已完成，单元测试 + 真机 7/7。**
- [x] 动作表 `ai_ops/actions.py`：10 个结构化动作（service.restart/reload/start/stop、file.write/append、log.rotate、disk.clean_temp、k8s.rollout_restart、shell.run），每条带 `auto_approvable`、风险等级、参数校验、基线拒绝路径。
- [x] 新工具 `propose_change(变更集)` 已**完全取代** `execute_command`（模型工具面已无命令字符串选项）。
- [x] 策略引擎 `decide(mode, step, trigger, host)` → 直接执行/发审批/拒绝；`direct` 只直过 `auto_approvable`；trigger 收窄到自己配置的动作且必须 direct；主机标签作用域。
- [x] 审批绑定载荷哈希，**含 params**；一组中任一步被拒则整组拒绝（原子性）。
- [x] Agent 侧 `changes.py`：独立重算 argv（与仓上 payload 不一致即拒）、重验哈希、基线路径拒绝、失败即停后续步骤。
- 真机验证（`scripts/smoke_change_host.py`，7/7）：写入真实文件成功；低权限 reload 失败如实上报；篡改载荷被拒且**未写入**；`/etc/shadow` 被拒；失败后后续步骤 skipped；k8s argv 形状正确。
- **真机发现并修复一个严重漏洞**：原 `payload_hash` 只哈希 action/program/args/identity，**不包含 params**。`file.write` 的内容全在 `params.content` 里，argv 只有路径——所以批准“写 A 内容”后换成恶意内容，哈希不变、校验通过。已在服务端与 Agent 侧同步修正并加回归测试。

## 阶段 3：模式固化与名单快照（主服务）
对应 §43.1、§44、§45.2 第 6 条。**已完成，单测 538 passed（新增 12）。**
- [x] 模式与名单在 `enqueue_turn` 时**一起固化**到 `role_turns`（`mode` + `readonly_list`/`write_list`/`deny_list` + `baseline_version`）；改绑只影响新任务。
- [x] 名单不可变快照：编辑=`fork` 新 ID 带 `parent_id` + `version+1`，父版本原样保留；`ensure_defaults` 幂等播种平台默认名单（只读工具一份、写动作一份），绑定三模式。
- [x] 规则带 `hosts` 标签作用域；`host_tags` 表 + `PUT /api/v1/assets/{id}/tags`。`propose_change` 先按名单过滤（`ACTION_NOT_IN_NAME_LIST`），再按标签作用域裁决（`HOST_OUT_OF_SCOPE`）。
- [x] 名单只能**收窄**不能放宽：命中名单仍要走动作表与策略引擎。
- [x] trigger/定时任务的 `mode` **必填**（缺失 422），保存时校验，不靠默认继承。
- [x] 平台基线黑名单 `ai_ops/baseline.py`：独立版本号 `2026-10-22.1`，15 类形态（PEM/OpenSSH 私钥、AWS、GitHub、Slack、OpenAI、Google、JWT、URL 内嵌凭据、YAML/JSON/键值命名密钥、Basic blob），接在 `scrubbing._redact_pattern` 最前面，**无需配置即生效**。
- 验收：`tests/test_stage3_modes_and_lists.py` 12 项（含“改绑不动已入队任务”“fork 不动父版本”“无标签主机不吃 tag 规则”“trigger 无 mode 被拒”）。
- 升级影响：schema 9→10（`backup.SUPPORTED_SCHEMA` 同步），迁移测试已更新。

## 阶段 4：凭证两档与 Agent 侧凭证存储
对应 §42、§46、§47。**已完成，单测 544 passed（新增 6）；真机 15/15。**
- [x] Agent 侧 `ai_ops_agent/credentials.py`：`credentials.json` + `credentials.key` 均 0600，AES-GCM（32 字节本机密钥，永不上行），原子写 + flock 互斥；句柄 `id` 由值 sha256 派生，重复发现不产生重复条目。
- [x] own/cooperative 按 **Agent↔主服务配对**（config 里 `own` 字段），不是 Agent 全局；`master_id` 唯一，重绑需显式解绑（`OWNER_ALREADY_BOUND`）；`share` 按 controller id 逐条放行；`revoke_controller` 一并清 ownership 与所有 share（被管方否决权，§46.2）。
- [x] 注入卡口：只有 `SINKS = {content, line}` 可放行，其余参数出现 `cred:` 直接 `CREDENTIAL_SINK_NOT_DECLARED`；未共享则 `CREDENTIAL_NOT_SHARED`——防止“把 cred 注进 curl”= 换名外泄。
- [x] 注入后二次脱敏：readonly 与 change 两条路径都用**凭证明文本地精确匹配**打码，不再依赖正则（§42.1 第 2 条）。
- [x] Agent CLI：`ai-ops-agent cred list|reveal|share|unshare|revoke|destroy`，全部只读本机配置、不需要主服务在线。
- [x] 主服务 `ai_ops/credential_handles.py`：只存句柄（id/name/note/source/kind/所属资产），**模型层无 value 字段**（多一字段直接 422）；误报否决 `dismissed` 永久生效，重扫不再冒头；句柄限本机可用（跨主机不授权）；`/api/v1/credentials|assets/{id}/credentials|history`。
- 真机（`scripts/smoke_credential_host.py`，15/15）：句柄不含值、双 0600、落盘无明文、reveal 可回、**另一账号 cat 被拒**、own/cooperative 流转、注入只走 sink、非 sink 参数被拒、输出里凭证明文被精确打码且非敏感行保留、flock 跨进程串行化（2.31s）。
- [x] **4.1 发现即凭证化**（已完成，主服务 548 passed，真机 14/14）：readonly 读取时 Agent 先发现再打码，`source` 取 argv 里实际读取的路径，句柄随结果上行，主服务在结果入口按资产登记为清单；模型侧只拿到 `credentials_found` + `credential_notice`，原文被替换而非补充。`Result.credentials` 用严格子模型，带 `value` 字段直接 422。顺手修了一个真 bug：无 turn 关联的任务上报结果时 `parent` 为 None 会 500（历史/直管任务会踩）。\n- 遗留：①connector 模式（无 Agent）仍走 `connector_secrets.py` 旧路径，尚未接入句柄表；②解密卡片逃生口（§42.1 第 4 条）待控制台；③句柄名目前是派生 hash，用户改名/改注释的接口待控制台。

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
- 2026-10-22 阶段 1 代码完成、单测全绿，**真机 smoke 未做**。主服务 508 passed（+20）；Agent 42 passed / 14 skipped。改动：新增 `ai_ops/readonly_tools.py`（主服务侧封闭只读工具集）、`ai_ops_agent/readonly.py`（Agent 侧独立复验 + execve 无 shell + 本机脱敏），`turns.py` 接上 readonly 工具分支，`authority.py` 允许 readonly 轮次的 claim，`execution.py`/`app.py` 支持协议 1.3 且不向旧 Agent 下发新 kind（审计去重）。遗留：③`cred:` 字面量注入属阶段 4；④**真机发现：低权限账号默认读不到 journal**，需 `usermod -aG adm`（已在 .94 建 aiops-ro 并加组，安装脚本必须包含这一步，否则 `journal_logs` 形同虚设）；⑤该主机非交互执行 PATH 为空且 `cat/mkdir/rm` 不在 /usr/bin，脚本一律用绝对路径并自行 export PATH；⑥`aiops-ro` 无 sudoers，所以 service.* 动作在真机上会被 OS 拒——这正确，sudoers 单条授权属阶段 5。
- 2026-10-22 阶段 3 完成。主服务 **538 passed**（新增 12）。新增 `ai_ops/namelists.py`（不可变名单/fork/绑定/主机标签）、`ai_ops/baseline.py`（平台基线黑名单，独立版本号 2026-10-22.1）。`enqueue_turn` 固化 `(mode, readonly_list, write_list, deny_list, baseline_version)`；schema 9→10，`backup.SUPPORTED_SCHEMA=10`。trigger/定时任务 `mode` 改为必填（缺失 422）。遗留：§44.2 的机械检查（死规则/相交/灾难性回溯干跑）与“默认名单有新版本”提示尚未实现，属阶段 6 控制台范围。
- 2026-10-22 阶段 4 完成。主服务 **544 passed**（新增 6）；Agent **52 passed / 25 skipped**（新增 11，Linux-only）。真机 `scripts/smoke_credential_host.py` **15/15**。新增 `ai_ops_agent/credentials.py`、`ai_ops/credential_handles.py`、`tests/test_credentials.py`、`tests/test_credential_handles.py`。依赖：Agent 仓新增 `cryptography`（pyproject 已需补上）。
- 2026-10-22 阶段 4.1 完成。主服务 **548 passed**；真机 `scripts/smoke_discovery_host.py` **14/14**（掩码、句柄不含值、source 取实际路径、二次读取不重复建条目、无密钥文件不产生句柄、清单文件无明文）。新增 `tests/test_credential_discovery.py`、`scripts/smoke_discovery_host.py`。
