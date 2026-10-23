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
- [x] `install-agent.sh`：建只读账号 + sudoers 仅放具体命令（--ops-units，无通配）；核心文件 600 属服务账号。审计表独立 DB 角色：SQLite 无角色概念，仅企业版 PG 部署清单项，不在开源版实现。
- [x] 外联出口说明与默认关闭自由 fetch（§41）：`search.EgressPolicy`，默认关且不向模型暴露 fetch_page；元数据/loopback 永久封；AI_OPS_FETCH_ALLOW/HOSTS。
- 验收：.94 真机从零安装，验收清单跑通。

## 阶段 6：控制台对接（Opus 5.5 子代理，主助手只给接口）
- [x] 后端补齐：`GET /api/v1/inbox`（审批/问题/账号申请三合一）、`POST /tasks/{id}/reject`（理由回给模型）、`PUT credentials/{id}`（改名/注释，证据字段不可改）、`/audit` 加 entity_id/event_prefix/actor/before 过滤、assets 列表带 tags。接口文档 `docs/governance-console-api.md`。
- [ ] 前端（Opus 5.5）：收件箱 / 名单与模式 / 凭证库 / 审计时间线，产物 `ai_ops/console/governance/`。
- [ ] 后端托管 `/governance` 路由。
- 验收：用户亲自审。

## 进度记录
（每阶段完成时追加：日期 / 改动文件 / 测试结果 / 真机证据 / 遗留）

- 2026-10-22 阶段 0 完成。基线：ai-ops 488 passed（3m50s）；ai-ops-agent 30 passed / 13 skipped（Linux-only 用例，Windows 跳过）。新增 `docs/protocol-v13.md`。本地仓与 GitHub 对齐（两仓此前落后远端 install.sh/CI 提交，已取回；本地未提交的 docs 已推送）。遗留：Agent Linux 用例须在 .94 真机跑。
- 2026-10-22 阶段 1 代码完成、单测全绿，**真机 smoke 未做**。主服务 508 passed（+20）；Agent 42 passed / 14 skipped。改动：新增 `ai_ops/readonly_tools.py`（主服务侧封闭只读工具集）、`ai_ops_agent/readonly.py`（Agent 侧独立复验 + execve 无 shell + 本机脱敏），`turns.py` 接上 readonly 工具分支，`authority.py` 允许 readonly 轮次的 claim，`execution.py`/`app.py` 支持协议 1.3 且不向旧 Agent 下发新 kind（审计去重）。遗留：③`cred:` 字面量注入属阶段 4；④**真机发现：低权限账号默认读不到 journal**，需 `usermod -aG adm`（已在 .94 建 aiops-ro 并加组，安装脚本必须包含这一步，否则 `journal_logs` 形同虚设）；⑤该主机非交互执行 PATH 为空且 `cat/mkdir/rm` 不在 /usr/bin，脚本一律用绝对路径并自行 export PATH；⑥`aiops-ro` 无 sudoers，所以 service.* 动作在真机上会被 OS 拒——这正确，sudoers 单条授权属阶段 5。
- 2026-10-22 阶段 3 完成。主服务 **538 passed**（新增 12）。新增 `ai_ops/namelists.py`（不可变名单/fork/绑定/主机标签）、`ai_ops/baseline.py`（平台基线黑名单，独立版本号 2026-10-22.1）。`enqueue_turn` 固化 `(mode, readonly_list, write_list, deny_list, baseline_version)`；schema 9→10，`backup.SUPPORTED_SCHEMA=10`。trigger/定时任务 `mode` 改为必填（缺失 422）。遗留：§44.2 的机械检查（死规则/相交/灾难性回溯干跑）与“默认名单有新版本”提示尚未实现，属阶段 6 控制台范围。
- 2026-10-22 阶段 4 完成。主服务 **544 passed**（新增 6）；Agent **52 passed / 25 skipped**（新增 11，Linux-only）。真机 `scripts/smoke_credential_host.py` **15/15**。新增 `ai_ops_agent/credentials.py`、`ai_ops/credential_handles.py`、`tests/test_credentials.py`、`tests/test_credential_handles.py`。依赖：Agent 仓新增 `cryptography`（pyproject 已需补上）。
- 2026-10-22 阶段 4.1 完成。主服务 **548 passed**；真机 `scripts/smoke_discovery_host.py` **14/14**（掩码、句柄不含值、source 取实际路径、二次读取不重复建条目、无密钥文件不产生句柄、清单文件无明文）。新增 `tests/test_credential_discovery.py`、`scripts/smoke_discovery_host.py`。
- 2026-10-22 阶段 5 主体完成。Agent **53 passed**（新增 test_service/test_readonly_identity，真机执行类测试 Linux 跳过）；真机 `scripts/smoke_stage5_host.py` **14/14**。①只读通道拒绝 root：`READONLY_USER_ROOT`，配置项 `allow_root_readonly`（默认关，装出无 root 账号的 allowed_users 会打警告）；②install-agent.sh sudoers 去通配符改为 `--ops-units` 精确单位列表，只读账号无 sudo、加 systemd-journal/adm 组；③真机验证不重启真实服务，sudoers 测试用一次性 oneshot unit。遗留：审计表独立 DB 角色（一行 GRANT，SQLite 无角色概念，仅 PG 企业版适用，已记入文档备注）；外联默认关闭自由 fetch 需与 §41 联动核对。
- 2026-10-22 阶段 5 收尾。主服务 **551 passed**（新增 3）。§41 自由 fetch 默认关落地。阶段 5 完成，进入阶段 6。
- 2026-10-22 阶段 6 后端完成。主服务 **558 passed**（新增 7，tests/test_inbox.py）。范围定稿：只做权限体系四个人工触点+审计视图，不做对话/资产/RBAC/在线编辑名单。前端已派 Opus。
- 2026-10-23 阶段 6.5 connector 打通 kind（补上阶段 1-4 的真实缺口）。**主服务 592 passed**（新增 34）。发现并修复一个严重设计缺口：`connector_ssh` 与 `connector_local` 都**无条件读 `payload['command']`**，而 `authority.claim_allowed` 会放行 `kind=readonly` 的任务，导致没装 Agent 的主机要么 KeyError 崩，要么落回 shell 字符串——**等于绕过闸1和闸3**。新增 `ai_ops/connector_kinds.py`：readonly 从 tool+params 重算 argv（复用 `readonly_tools.PROGRAMS`，不建第二份表），change 逐步重展开并重算审批哈希，任何不一致一律 fail closed。SSH readonly **一律不 sudo**（提权读本身就是这个通道要禁的事）；change 首步失败即停并如实上报第几步跑了。local connector 支持 readonly，但**故意拒绝 change**（控制主机跑着控制面，批准一次写入等于批准自我修改）。顺带修了 `run()` 里“先取连接信息再校验”的顺序问题。
- 2026-10-23 阶段 6.6 敏感解锁卡（§51.4）。**主服务 602 passed**（新增 10）。新增 `ai_ops/sensitive.py`：readonly 结果里出现掩码即生成 `sensitive_hit`（只存规则 id + 路径，**绝不存值**，所以解锁只能回主机重读），`POST /sensitive-hits/{id}/unlock` 发短时 token、`GET .../revealed` 一次性消费；串进 `/api/v1/inbox` 与审批并列。硬约束都在测试里钉死：**没有面向模型的解锁工具**（有测试断言动作表/只读表里不含 unlock/reveal/sensitive 字样）、确认路径不符不解锁、token 错不给、不可重用、过期即失效、非 admin 拒绝。
  遗留（描述清楚，不是假装做完）：`GET .../revealed` 目前只能返回控制面持有的**已脱敏**副本，会如实告诉操作者“去主机上重读”，而不是伪造一个 reveal。真正的原文回显需要 Agent 支持二次读取执行——属下一阶段。
- 2026-10-23 阶段 6.7 名单机械检查与平台默认漂移（§44.2、§44.1）。**主服务 641 passed**（新增 32）。新增 `ai_ops/namelist_checks.py`：死规则（被更宽规则完全覆盖）、跨名单冲突（allow/deny 同动作）、干跑（候选动作→哪条规则裁定）。作用域比较是**精确子集而非启发式**：`[]` 表示全部主机，`[a,b]` 因为要求两个标签都在，所以反而比 `[a]` **更窄**——这点写反了就会漏报死规则，已专门钉测试。冲突不自动裁决，而是报出来（引擎固定“禁止优先”是对的，但“我明明允许了还是被拦”会耗尽信任）。干跑**只返回规则 id 与裁定，进岀都没有文件内容**，有测试断言返回字段集合。平台默认漂移：`default_drift()` 拿代码定义对比库里快照，报新增/撤销/变更；**任何情况都不自动改绑**（自动改绑等于允许安全补丁不问就改权限），接受变更走 `apply_default_drift()`，fork 带 `parent_id` 再改绑，审计能回溯。
- 2026-10-23 阶段 6.8 重绑影响面（§44.3 末句）。**主服务 649 passed**（新增 8）。新增 `ai_ops/binding_impact.py`：§44.3 原文要求提示“N 个任务仍在用旧名单”，但真实风险恰好相反——**运行中的轮次是安全的**（入队时已固化名单 ID），**危险的是定时任务**（只存模式，触发那一刻才解析绑定）。接口按模式报：绑定的名单 ID、绑定多久前改的、会受影响的定时任务清单、以及“运行中轮次不受影响”的计数（分开列，故意不混）。只是告警面，**不阻止重绑**——阻止一次有意为之的改绑比告诉操作者它影响什么更糟。
- 2026-10-23 §45.1 第 2 条（脱敏视图）核实结论——**当前实现与设计文本不同，但预言机风险在当前工具集上不成立**。核实依据：`readonly_tools.PROGRAMS` 只有 10 个工具（cat/ls/stat/df/free/ps/systemctl/journalctl/kubectl），**一个可被模型控制的谓词都没有**（没有 grep/wc/find/head/awk），参数只能选路径、行数、unit 名、namespace，**不能影响“读哪些字节”或“怎么裁剪”**。所以 §45.1 第 1 条说的“逐字符二分猜密钥”在这里没有载体：模型要么拿到整个文件（已在 Agent 侧整段脉做），要么拿不到。
  与 §48 的真实差异：§48 要求“工具作用于脱敏后内容”，当前是**工具先跑完、再对输出整段打码**。对现在的工具集两者等价（cat 输出就是文件全部）；但**一旦将来加 grep/wc/head 这类谓词工具，先跑后打就会变成预言机**。结论：现阶段不是漏洞，是设计债——加谓词工具之前必须先把 `read_sanitized` 做出来，否则就是自己挖的坑。已写入待办而不伪装成已完成。
- 2026-10-23 阶段 6.9 Agent 仓对账（两仓接口一致性）。**Agent 106 passed / 32 skipped**（新增 53，其中 31 条是一致性用例）。发现并修复一处真实接口不一致：主服务新加了 readonly payload 里的 `params`（原本为了 connector 能在没 Agent 时重算 argv），但 **Agent 侧完全忽略 `params`**，只校验 `args` 白名单——两边靠惯例对而不是靠构造对。现在 Agent 自己实现 `derive_argv()`（**故意不 import 主服务的**，§39.2：从被检查方 import 检查代码等于没检查），不一致就 `ARGV_NOT_DERIVED_FROM_PARAMS` 拒。无 `params` 的旧任务不受影响（argv 白名单仍在，开口没变宽）。
  新增 `tests/test_cross_repo_parity.py`：15 个用例 + 程序表必须两仓逐一相等，找不到主服务源码时 skip 而非 fail。**验证过它真会咬人**：把 Agent 的 `df` 路径改成 `/bin/df`，立刻 2 条断言红。
  顺手钉死一个容易改坏的顺序依赖：`changes.py` 里**凭证注入必须发生在审批哈希校验之后**（哈希要盖住人批准的占位符，而不是注入后的明文），已加测试。
  核对通过无需改动的：①`change` 的 params 本就参与重展开与哈希；②`redact` 只作用于 stdout/stderr，不会误伤 `credentials` 键；③Agent 把整个 result 原样上报，句柄能到主服务。
- 2026-10-23 阶段 6.10 解锁真回显打通（跨两仓，原待办①完成）。之前的 `revealed` 只能返回打码副本，根本原因是 `report_result` 对**所有**结果调 `redact_result`，reveal 的明文在入口就被抹掉了。
  修法（四处，缺一不可）：①Agent 新增独立 kind `reveal`（**不是** readonly 上加 flag——flag 会被构造 readonly payload 的那一方决定，而那方是模型）；②`report_result` 对 reveal 开唯一例外不做脱敏，但**不写 `tasks.result`**，改写 `reveal_secret` 独立表（`tasks.result` 就是模型看到的工具输出，写进去等于把明文塞给模型）；③`revealed` 改成两态：首次请求派发 clone 任务返回 202，授权**不**消耗；拿到字节才销（删 stash + 改状态）；④`authority.claim_allowed` 给 reveal 单开分支（它 `turn_id IS NULL`，走不了原 turn 校验，改用 `source_turn_id` 复核 + 15 分钟新鲜度）。
  排队规则：reveal **不插队**——只在 `row is None`（没有模型任务可领）且无在飞任务时才领。
  Agent 侧额外收紧：reveal **只允许 `read_file`**（目录列表/journal 流全量解锁是另一个功能、另一个威胁模型）；**root 一律拒绝**，不提供 `allow_root_readonly` 旁路；输出上限 256KB；不回传 credentials 句柄（看一眼就入库等于副作用）。
  测试：主服务 652（+3），Agent 111（+5）。新增 `test_reveal_bytes_never_land_in_the_task_result` 直接断言明文不在 `tasks.result` 里、且在 `reveal_secret` 里、取回后行被删。
- 2026-10-23 顺手堵一洞：`turns.py` 的 `clean_params` 原样拷贝模型传入的**任意**键进 payload。`kind` 目前硬编码所以不可利用，但“模型能写的 payload 字段”与“派发器写的字段”同名，一次「合并 payload」重构就能把 kind 变成模型可选。已加 `_RESERVED` 黑名单。
- 2026-10-23 阶段 6.11 解锁链路的端到端演练（`scripts/rehearse_reveal.py`）。**当场拓出一个真 bug**：reveal 的 clone 原来直接继承原任务的 `args`。但存量任务 payload 里不一定有 `args`（发字段后来才加的），于是真机上 Agent 会“没东西可执行”而在主机端失败——**离原因很远，而且是在出事时**。改为 clone 自己用 `readonly_tools.build(params)` 重算 argv，推导不出就 409。
  演练脚本走真实 HTTP（建资产/建角色/记录命中/unlock/派发/领取/上报/取回/审计），然后直接查库核实明文去向：不在 `tasks.result`、在 `reveal_secret`、取回后删除、审计流里没有。退出码即结论。
  两条测试纳入套件：一条跑正常链，一条**把 bug 重新引回去**（stash 前先脱敏）验证它真会报红。**不会失败的检查等于装饰**。
- 2026-10-23 待办：①`read_sanitized` 中间层——**明确结论：当前工具集无谓词工具（grep/wc/find 全在禁用表），§45.1 预言机攻击不成立**，此条仅为“将来加谓词工具”的前置条件，现在写就是空壳，不做；②前端最后统一由用户重做。
