# OpenClaw 复用与裁剪评估（P2 前置调研）

更新：2026-10-03。本文用于把「OpenClaw 二开路线未定」推进到一个**可决策**的状态。
它区分三类内容：**已核实事实**、**建议**、**尚未验证**。建议不等于用户已同意。

## 1. 已核实事实（来自本机 EasyClaw 1.3.108 发行包）

- 运行时是一个 **Node 网关进程（Gateway）**，默认 `127.0.0.1:18789`，通过 **WebSocket** 暴露请求/响应/服务端推送事件（事件如 `agent`/`chat`/`presence`/`health`/`heartbeat`/`cron`）。
- 许可证 **MIT**（`docs/reference/credits.md`：`MIT - Free as a lobster in the ocean.`）。上游作者 Peter Steinberger / Mario Zechner 等。
- 多**渠道**：内置 Telegram/Slack/Discord/Signal/WhatsApp/iMessage/IRC 等，插件渠道含飞书(Feishu)/LINE/Matrix/Teams/QQ/微信等。渠道可插件化、可只是部分选择。
- **多 Agent 路由**：按 workspace / sender 隔离会话；`main` 会话直接聊天，群聊隔离。
- **Agent 循环**：入口 `agent` / `agent.wait`；`agent` 立即返回 `{runId, acceptedAt}`；`runEmbeddedPiAgent` **按会话 + 全局队列串行**，有超时中止；事件流分 `tool`/`assistant`/`lifecycle`。
- **上下文引擎可插拔**：内置 `legacy` 引擎负责「包含哪些消息 / 如何摘要旧历史 / 子 Agent 边界」，可通过插件槽 `plugins.slots.contextEngine` **整体替换**。
- **记忆**：纯 Markdown 文件，长期 `MEMORY.md`、按天 `memory/YYYY-MM-DD.md`，另有 `memory_search`（语义检索）/`memory_get`。与「按天全文/压缩/梗概分层」需求结构同源。
- **凭据/秘密**：SecretRef（env/file/exec），运行时快照 + 启动失败即快速失败 + 重载原子交换（成功或保留 last-known-good）；仅对**生效中**的表面做校验。
- **密钥/主数据**：配置在 `~/.easyclaw/easyclaw.json`；工作区在 `~/.easyclaw/workspace`。
- 本机为 Electron 打包，`gateway.asar`（约 398MB）未解包，源码不在本机；本机可见的是**发行文档 + 配置/工作区数据**，不是可编译源码树。

> 结论性事实：OpenClaw 提供的是**渠道 + Agent 循环 + 会话 + 记忆 + 秘密管理**，是一个「消息→Agent」网关；它**不**提供我们已有的「资产注册 + 受控命令执行 + 任务队列 + 审计/租约/备份」那块运维执行底座。

## 2. 集成边界建议（供决策）

三种路线：

- **A. 深度 fork（改 asar 内部源码）**：能改内核，但 398MB 打包产物、无源码树、上游持续演进 → 维护成本极高，且与「轻量皮实」目标冲突。**不建议**作起点。
- **B. 插件式复用（推荐起点）**：把 OpenClaw 当**消息网关 + Agent 宿主**，通过其**公开边界**接入，不碰内核：
  - 用 **context engine 插件槽**接管上下文组装（实现我们要求的「接口文档全量强制注入 + 核心文档强制注入 + 按天分层记忆」）。
  - 用**渠道插件**接入飞书/微信；用现有渠道做入口。
  - 主服务作为**旁路 RPC/HTTP 客户端**与 Gateway 通信（`agent`/`agent.wait`/会话），不把编排逻辑塞进 Gateway。
  - 我们的执行底座（资产/队列/审计/租约）保持在 ai-ops 主服务里，Gateway 只是「对话与渠道面」。
- **C. 自研替代**：完全不用 OpenClaw，自己写渠道与 Agent 循环。最可控但工作量最大，且要自己解决渠道适配（飞书/微信等）与上下文引擎。

**推荐**：先按 **B** 落地，把「必须改内核」的部分压到最小；只有当某个能力在插件边界做不了时才评估 A。

## 3. 尚未验证（必须先做验证再定集成边界）

- 上下文引擎插件槽能否**完整**满足：全量接口文档注入 + 核心文档强制注入 + 无重叠日期边界的全文/压缩/梗概分层 + 旧记忆按需加载原文。**未验证**，需要写一个最小 context-engine 插件实测（`easyclaw plugins install -l`）。
- 渠道插件（飞书/微信）能否按「同一角色跨入口共享记忆」的要求路由到**同一会话/同一 Agent**。文档说「按 workspace/sender 隔离」，DM 默认共享 session；跨渠道同一角色映射需**实测**。
- Gateway 的 `agent` RPC 是否允许**外部队列串行约束**（我们要「一个角色同时一轮」），还是要依赖其内置的 per-session 队列。**未验证**。
- 秘密边界：SecretRef 只用「生效中表面」校验；我们需要确认「角色执行账号」是否也走这条线，或继续由 ai-ops 主服务持有（当前主服务已 DPAPI/文件挂载）。
- 许可证合规：MIT 允许闭源/商用，但需保留版权与许可文本；**分发二开产物时的署名义务**要在发布前核对。

## 4. 与 P2 其余条目如何衔接

- **聚合站模型**：Gateway 支持任意 OpenAI 兼容端点；可让 Gateway 指向 aishuch 或走主服务代理。Key 仅经秘密通道注入，不进模型上下文。
- **角色串行多步骤工具循环**：先由 ai-ops 主服务实现（已有 docs/model-turns.md），与 Gateway 的会话串行语义对齐；避免双份编排。
- **全量接口文档注入 / 按天分层记忆**：优先做成 **context-engine 插件**；若插件边界不够，再退回主服务侧预组装上下文并作为单条消息喂给 Gateway。

## 5. 下一步（建议的最小验证，1 个迭代内）

1. 用 `easyclaw` CLI 起一个本地 Gateway，注册一个 **OpenAI 兼容 provider**（指 aishuch），跑通 `agent` RPC 一轮带工具调用的循环。
2. 写一个最小 **context-engine 插件**，注入一段「接口文档 + 核心文档」，用 `/context detail` 验证确实进入上下文。
3. 验证 **feishu 渠道插件**能把消息路由到指定 Agent/会话，且与本地聊天共享同一角色记忆。
4. 产出验证报告，据此把 P2 的「OpenClaw 集成边界」从**未定**改为**已定**。

在完成第 1–3 项前，不应把 OpenClaw 集成写成「已完成」，也不应把 Python 执行底座称为整个编排选型。
