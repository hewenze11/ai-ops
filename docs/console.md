# Web 控制台（P3 第一块）

更新：2026-10-03。P2 后端（模型/角色/记忆/告警）联调达标后进入 P3，用户已确认先做
**Web 前端 + 渠道**，并从**最小可用控制台**切入。本次落地的是控制台第一版。

## 定位

- **只读优先**：这一版的目标是让运维**亲眼看到后端跑起来**——聊天、审批、任务/轮次/
  告警/审计可见。**角色与资产注册仍是管理员 API 操作**（不是 Web 向导），符合既有
  「资产供给是部署动作」的边界。
- **零依赖、零构建**：单个 `index.html` + `app.css` + `app.js`，原生 JS，不引入框架
  和打包器。这样镜像不增加 node 工具链，也不会引入前端依赖供应链。
- **后端内嵌**：由 `ai_ops/console.py` 用 `FileResponse` 直接托管静态文件，与主服务
  同进程、同端口。

## 凭据处理（重要）

- 管理员 token **只存在于当前标签页的内存变量**里。**不写** localStorage /
  sessionStorage / cookie / URL。刷新或关闭标签页即失效；**其他标签页读不到**。
- 页面通过 `Authorization: Bearer` 头调用既有管理员接口；**静态资源本身不需要凭据**，
  所以 `GET /` 公开可见，但**没有任何数据接口可以在无凭据时返回内容**。
- 这是刻意的取舍：把"浏览器里放一个长期凭据"这件危险的事降到最低，代价是每次刷新
  要重新粘贴 token。后续若做登录，应引入会话/短期凭据，而不是把 token 存起来。

## 前端安全姿态

- 所有动态内容用 `textContent` 插入，**绝不 `innerHTML`**。告警 payload、命令输出、
  文档正文都是不可信数据，不可能借控制台注标注入。
- 控制台**不伪造执行结果**：它只照实渲染后端返回的状态，包括 `unknown` 和
  `awaiting_approval`——不会把"已提交"显示成"已成功"。

## 页面

| 标签 | 内容 | 依赖接口 |
| --- | --- | --- |
| 聊天 | 选角色 → 发消息（选模式/账号）→ 看轮次、模型调用、审批/取消 | `/api/v1/roles`、`/roles/{id}/messages`、`/turns/{id}`、`/turns/{id}/model-calls`、`/tasks/{id}/approve|cancel` |
| 控制台·总览 | 计数与**如实列出的缺口** | `/healthz`、`/api/v1/console/overview`、`/operator/attention` |
| 控制台·需人工处置 | 未知执行/陈旧租约/离线资产；未知执行可**带核实说明**处置 | `/api/v1/operator/attention`、`/tasks/{id}/resolve` |
| 控制台·告警日志 | 最近告警 + 按来源计数 | `/api/v1/alarms`、`/alarms/sources` |
| 控制台·任务 | 最近任务（含命令、状态） | `/api/v1/console/tasks` |
| 控制台·角色 | 角色列表 + **创建 / 改名** | `/api/v1/roles`、`/api/v1/console/roles`、`PUT /roles/{id}` |
| 控制台·角色轮次 | 按角色查看轮次 | `/api/v1/roles/{id}/turns` |
| 控制台·定制任务 | 触发/定时任务与投递箱 + **创建 / 编辑 / 删除** | `/api/v1/custom-tasks`、`/api/v1/console/custom-tasks*`、`/schedule-deliveries` |
| 控制台·触发器事件 | 事件队列 | `/api/v1/custom-task-events` |
| 控制台·渠道 | 渠道身份与一次性配对码签发 | `/api/v1/channels/identities`、`/channels/pairings` |
| 控制台·文档 | 核心文档列表 + **编辑 / 删除** | `/api/v1/documents*` |
| 控制台·资产 | 资产列表 + Agent 在线状态 + **注册 / 编辑** | `/api/v1/assets`、`/api/v1/console/assets`、`/agents/{id}/status` |
| 控制台·Skills | 本地 Skills + **新建 / 编辑 / 删除** | `/api/v1/skills*` |
| 控制台·记忆 | 按天分层记忆与策略 | `/roles/{id}/memory*` |
| 控制台·审计 | 全量审计 | `/api/v1/audit` |
| 控制台·输出 | 归档用量与保留策略 | `/api/v1/output/usage` |

## 新增后端接口

- `GET /api/v1/roles`：角色列表（此前只有创建接口）。
- `GET /api/v1/console/tasks?limit=`：最近任务列表（任务只有单条查询接口）。
- `GET /api/v1/console/overview`：汇总计数 + **明确列出的未完成项**（模型梗概占位、
  渠道未实现、角色/资产注册非 Web）。这个"缺口清单"是刻意的，避免控制台看起来比
  实际更完整。

## 部署与可达性

- 控制台与主服务同端口：容器内 `8765`。`compose.yaml` 的端口绑定改为
  `AI_OPS_BIND:AI_OPS_PORT:8765`，默认仍是 `127.0.0.1`。要让测试机浏览器访问，
  设 `AI_OPS_BIND=0.0.0.0`——**只有当主机不对公网可达时才这么做**。
- 默认监听回环，符合"先别把管理面暴露出去"的保守默认。

## 尚未实现（明确边界）

- **在线编辑子集之外仍走管理员 API/CLI**：agent token 轮换、SSH 密钥、角色模型配置
  不在浏览器里改（见 docs/management.md）。
- **并发编辑冲突检测**未做（后写覆盖，有修订号可对比）。
- 前后端分离、前端框架、TypeScript、组件库均未引入——这一版刻意不引入。
- 登录/会话/多用户/权限分级均未做：控制台是**单管理员凭据**的运维面。
- 飞书/微信的**真实出站发送与平台签名校验**属部署侧，本仓不内置也不宣称已验证。

## 测试

`tests/test_console.py`（4 项）：静态资源公开且非空、脚本内**不得含凭据**、
所有数据接口无凭据时 401/403、角色列表与任务列表、总览计数与缺口清单。

本地：`pytest` 188 通过 / 1 跳过（Windows 文件锁下 skip 的备份恢复用例，见下）。

**一个实操教训**：备份恢复用例在 Windows 上会因"运行中的服务/句柄仍持有 SQLite 主文件"
而无法重命名。生产上恢复本就是**停服务后**的 CLI 操作；测试如实体现这一点（先停服务，
再重试重命名，仍被占用则跳过而非假绿）。备份恢复代码本身也补了：
- `fsync` 用可写句柄（Windows 只读句柄 `fsync` 报 EBADF）；
- 恢复前额外尝试退出 WAL 模式（尽力而为，被占用则忽略，因 checkpoint 已让主文件自洽）。
