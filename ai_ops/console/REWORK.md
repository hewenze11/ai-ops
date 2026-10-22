# 运维工作台重整说明

本目录为原有真实产品前端，不是 mock 原型。生产页面不引入第三方依赖；所有读写仍通过带管理员认证的现有 API。

## 信息架构
- 顶栏：资产、聊天、定制任务、Skills、记忆、文档、日志。默认资产。
- 资产：服务器卡直接提供“编辑名称 / 备注”、连接设置、注销 / 恢复、删除。注销原卡保留标“已注销”；删除需输入ID，服务端保留执行中/未决保护。无页底生命周期面板；本机纳管开关在本机卡设置。首次未注册本机也保留诚实的设置入口，不伪造在线状态。指标/ACL/本机权限集中于折叠说明。
- 聊天：创建与设置都可继承默认接口或配置独立 Base URL / API Key / 模型；高级折叠放运行上限。消息渠道统一归角色，扫码位置标“待接入”无假码；配对码与边界在高级折叠，明确不是扫码登录。审批、未决核实、轮次取消仍在聊天；保持简洁气泡与折叠步骤。
- 定制任务：Webhook只跟随任务配置及测试；移除页外全局配置块。新建默认明确关闭；旧null配置显示“旧任务兼容：沿用原全局通知配置”，不暗改旧路由。通知worker停用时显示真实警告。
- Skills：内容编辑与卡片下角色绑定/启用；无角色绑定不会宣称可用。
- 日志：任务、告警、触发、审计和命令输出使用服务端统一日志游标分页，支持来源/角色/资产服务端筛选；关键词仅筛选已加载记录。旧通知/定时投递保留在独立折叠区，各取最近100条。原“输出”页的归档容量/保留策略归此页，任务详情可分块读取 stdout/stderr。

## 与后端约定
- 资产卡和心跳资产筛选使用`GET /api/v1/assets?include_retired=true&include_disabled_local=true`，按`retired_at`或`retired`识别已注销，停用本机使用`health.health_state=disabled`。直接消费各资产内置`health`，一次请求完成整批刷新。执行选择与派生清单仍使用活动资产口径，不扩大执行范围。
- `heartbeat`：使用 `observation_window_seconds` 与窗口计数/比率，非 `expected_interval_seconds:null`。卡片一行“最近10个10秒窗：8/10有心跳；缺失20%（非发送丢包率）”，统计不全显示“待采样”。页级折叠区保留完整时间窗缺失率口径、实际发送次数不可用、非实际尝试丢失率/非网络丢包率说明；分母为实际观察窗数，最多10窗。
- local 卡片仅留“Docker 下执行于控制服务容器，不代表宿主机可控”；worker 存活、凭据与数据库同权风险、宿主 Agent/SSH 管理方式在页级折叠区说明。
- `GET /api/v1/logs?kind=&role_id=&asset_id=&before=&limit=100`：`items` / `next_before`，`all`在服务端排除心跳；UI来源选择不再提供逐条心跳大卡。游标不解析，仅 URL 编码回传。任务/输出用 `entity_id` 补取真实任务详情。
- `GET /api/v1/logs/heartbeats?limit=100&asset_id=&before=`：唯一连续“心跳记录”折叠区，首次展开加载，分页重绘同一张表。单资产列为时间/信号，多资产增加资产列。严格使用`state_code`，null显示未知；4标TTL观测推导。ACK仅高级详情入口。
- `RoleModel` 创建嵌套`model`、设置PUT`/roles/{id}/model`：`provider_mode=inherit|independent`、`base_url`、`model`、`api_key`仅填写才提交；只消费`api_key_configured`，不回填秘密。password输入，关闭/成功/失败后清除；密钥写入错误使用通用文本防验证回显。明确清除按钮经确认后进入待保存状态，提交`clear_api_key=true,enabled=false`且不同时发送`api_key`；待清除时禁用密钥与启用开关，可取消清除但不自动重新启用模型。独立接口旁明确仅支持公网HTTPS:443/OpenAI兼容。`max_model_steps=null`不限，新建默认；存量限额回填保留；`max_output_tokens=2048`和`max_context_chars=250000`仍有限。
- `GET /api/v1/assets/{id}/heartbeats?after=...&limit=100`：`items` / `next_after`；24h保留，分页读取。
- `CustomTask.notification`：三态选择，`null` 沿用旧全局回退，`{enabled:false}` 明确关闭，`{enabled:true,...}` 为本任务结果 Webhook。编辑 URL 留空时省略 `url` 字段保留原秘密，不把脱敏地址填入输入框。任务通知不自动订阅资产失联。
- `GET /api/v1/operator/attention`：消费 `unknown_executions`、可选 `awaiting_approvals`、`stale_leases`。unknown 列表允许旧格式缺 state、payload 为 JSON 字符串。
- 系统资产清单通过 `GET /api/v1/documents/system-asset-inventory` 单独获取并去重加入文档列表，`read_only` / `derived` 只读；核心资产清单对全部角色可见，账号交集不构成资产读 ACL。

## 验证
```powershell
node --check .\ai_ops\console\app.js
node .\ai_ops\console\console.test.cjs
```
测试无外部依赖，以隔离 DOM/API fixtures 覆盖前两轮所有回归，并增加独立Provider/继承、密钥留空与清除、存量限额/新建不限、渠道归属和无假码、卡内编辑/注销恢复删除确认、派生文档跳真源、新任务通知关闭/旧继承、心跳单表翻页/资产筛选/未知和TTL标注。测试fixture不进入生产页面。未调用真实浏览器、真实外部接口或Webhook，不宣称浏览器视觉验收。

## 限制/后续真机验收
- 集成待验（2026-10-09）：已按父会话确认的`include_retired=true&include_disabled_local=true`契约完成两个列表入口与DOM fixture；后端app.py由负责人唯一修改并全量集成验证。组件测试不代表真实后端已部署。
- 尚未做真实浏览器截图与浏览器端端到端测试；DOM 冒烟不是视觉验收。
- 统一日志、心跳、输出归档均支持继续读取；兼容投递折叠区仅各来源最近100条。
- 资产每5秒刷新一次列表，使用列表内置健康状态，不逐资产发起状态请求。
- 若后端尚未提供 `attention.awaiting_approvals`，没有关联当前最近100轮次、且已落在最近100任务之外的独立审批不可被当前前端发现；必须补后端完整待审批列表，不能靠取消或扩大静态 limit 替代。
- SSH 不提供 Agent 心跳，显示未探测；本机依据真实工作线程状态，绝不伪造心跳。
- Docker 下 local 是控制服务容器，不是宿主机；管理宿主机须宿主 Agent/SSH。local 服务账号可能接触控制面凭据/db，不构成安全隔离。
- 当前无角色资产读 ACL：核心资产清单对全部角色可见，模型每次重建全部活动资产清单；仅 available_selected_users 按本轮所选账号与资产允许账号取交集。未选账号不隐藏资产，只是可用执行账号为空。core 文档跨角色注入；命令还受 OS 权限限制。Skills role_ids=[] 则任何角色都不注入，不是全局启用。
- 模式描述遵循服务端：confirm 每条命令审批，readonly 无命令执行工具，direct 不自动拦截重大或写操作；操作系统账号权限是最后边界。
