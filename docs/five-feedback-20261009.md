# 2026-10-09 五项体验反馈交付说明

## 实施范围
本次只更新 .94 控制服务；不修改 .239 Agent、不连接 .19、不 push GitHub/GHCR。保留原资源ID、角色 h1（ID 11）、已有 queued 消息、凭据和用户数据。前端由用户自行体验，验证为 Node DOM 组件测试、Python API 测试及真实服务只读 API，不包含真实浏览器验收。

1. **角色与模型**：新建角色时可启用模型、输入模型名（留空继承全局），角色和模型在一个事务创建。角色列表增加文字“设置”。设置弹窗显示全局默认模型、worker 实际存活状态和配置边界。真正运行的主服务对未启用/未启动worker/没有有效模型名的新聊天返回409，不入队；旧 queued 消息不删除也不自动改配置。Base URL/Key 仍属于全局 Provider：部署环境 `AI_OPS_MODEL_BASE_URL`、`AI_OPS_MODEL`；Key 经 `AI_OPS_MODEL_KEY_FILE` 只读秘密文件加载，浏览器不返回/保存Key。配置变更由部署管理员完成并重启；本次不增加Web端密钥管理器，亦未做模型连通性付费请求。
2. **紧凑聊天**：用户与助手独立紧凑气泡，不再每句套巨大轮次卡。轮次时间、ID、模式、账号、普通任务信息、完整轮次/模型调用/步骤放默认折叠“运行详情”。审批与 unknown 处置卡、错误/取消终态及取消按钮继续可见。
3. **自动ID**：角色/资产/Skills/文档/定制任务创建端服务生成 UUID ID；旧显式ID/API PUT/导入继续支持且不改旧ID。新增 Skills/文档 POST 为只创建语义，碰撞返回409而不是覆盖；其他创建路径既有冲突检查保留。前端隐藏ID输入，以名称/类型为主；增加名称/类型/ID搜索、详情复制。已有记忆按日期和角色管理，执行任务/配对码/告警/轮次原本自动生成，不新增人为ID。
4. **机器连接与状态**：资产卡明显“连接设置”。Agent 提供接入说明/显式确认token轮换；SSH提供地址、用户、认证类型、服务端秘密引用/固定公钥编辑、仅手动握手测试；local说明worker/容器边界。飞书/微信等消息渠道仍归属角色，资产弹窗明确引导，不引入资产消息渠道绑定。卡片移除心跳日志按钮，统一日志增加heartbeat来源与按资产筛选、发送序号/ACK详情入口。
   - 新增24h真实接收状态样本，卡图取最近100条，1=在线空闲、2=本主执行、3=他主执行、4=收到可信状态后超过30秒TTL的断联观测。判断依据1.2协议busy/busy_by及telemetry.controller（无telemetry时用已观测claim身份），不依赖排队数量。缺身份的busy/旧1.1协议为未知，不补造历史；SSH不画Agent图，local只说明worker。
   - **未统一Agent心跳周期**：现有空闲循环睡3秒、执行期间监视循环等待2秒、异常循环5秒，并叠加网络和多主服务串行访问耗时；10秒只是旧观察窗口，30秒为TTL。统一需要改Agent并重启受控端，本次用户允许不做，因此未修改 .239。
5. **任务Webhook**：不再藏于details，地址输入始终可见，明确“沿用全局/关闭/本任务”三态。保存地址留空保留原秘密；测试可以使用已保存地址，不回传原URL。新增管理员鉴权真实POST测试接口，沿用禁止私网/回环/元数据、DNS检查、禁重定向、超时和有界读取，返回HTTP状态不声称收件人收到；点击才发一条测试，不自动测试任何已有URL。runtime接口显示通知线程是否真实运行。.94既有 `AI_OPS_ALERT_NOTIFY_ENABLED=0` 保持不变，界面明确自动通知不可用，不擅自启用并补发旧队列。

## 验证与限制
- 本地全量 Python：325 passed, 1 skipped（平台相关）；Node DOM组件测试通过；另保留现有Webhook安全回归。
- 准备部署时再次在安装包上运行完整API测试、校验包内所有生产文件SHA256，再备份并切换 .94。
- 不将Provider配置存在等同模型调用成功；不将HTTP 2xx等同机器人业务接收成功；不将心跳观测等同执行结果。
- 现有测试入口明文HTTP无TLS风险仍在，不适合作为生产安全入口。
- SQLite新增状态样本表是增量迁移，不重写用户数据。状态样本为监控遥测，按24h保留；既有业务数据保留规则未改。

## 实际部署证据（2026-10-09 12:02 CST）
- 公网入口：http://172.236.254.94:8765/；UI指纹 `a3aaafecd6358a66`，首页/指纹静态响应均 `Cache-Control: no-store`。
- 镜像：`ai-ops-service:ui-20261009t040017z`。全36个生产包文件与源码 SHA256一致，无不匹配；安装包隔离环境全量测试 **326 passed**。本机 Node DOM测试通过；没有浏览器验收。
- 公网JS SHA256：`74bfdf3687bef1c22c80f29a922ecd7b26cf171fb597afc1e3e9b966455aad9b`。
- 公网CSS SHA256：`17e6c5edf83a8d151e94ba4e74ee92a15deb2aeaa1ddbd17e7e0199a7e6d6e61`。
- 停服务一致性备份：.94 `/opt/ai-ops/backups/pre-ui-20261009T040017Z.tar.gz`（0600），compose另存 `/opt/ai-ops/backups/compose-pre-ui-20261009T040017Z.yaml`。
- 回滚镜像：`ai-ops-service:pre-ui-20261009t040017z`（原镜像 sha256:2c88805617859c5861c77902e5a43cf8e202ada84e90d9ba2cdded604d6bd359）。回滚先核实无claimed任务，恢复备份compose后使用 `docker compose ... up -d --no-build --pull never control`，通常无需覆盖业务库（新增表向后兼容）；不要直接覆盖已有新用户数据。
- 部署前后 roles/assets/role_models/asset_connections/asset_lifecycle/local_host/skills/documents/custom_tasks/role_memory_policy/channel_pairing/channel_identities/alert_notify_config 逐表相同；admin/model秘密文件字节不变；role_turns/tasks/model_calls 逐表相同。h1=角色11仍有1条queued，未擅自启用模型或补跑。
- 真实API：模型worker运行、Provider已配置，默认 `gpt-4.1-mini-2025-04-14`；通知worker关闭且前端显示限制。agent-239真实接收状态1、observing_controller=main-94；local worker健康；统一heartbeat日志已经接收到新样本。
- 验证文件：`C:\Users\Administrator\.easyclaw\workspace\scratch\ai-ops-development\frontend-release-20261009\manifest.json`、`verification.json`、`five-feedback-live-proof.txt`。未向任何未知Webhook发送消息、未执行真实运维命令。
