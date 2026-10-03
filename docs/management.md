# 管理写入面：角色 / 资产 / 文档 / Skills / 定制任务

更新：2026-10-03。控制台此前是**只读优先**；本模块补上运维在浏览器里真正需要的
**写入操作**（`ai_ops/management.py`），同时**不发明新权限**——每条路由都要求和管理员
接口相同的凭据，且**没有任何一条能在主机上执行命令**。

控制台专用的新路由统一挂在 `/api/v1/console/*`，与核心管理接口共用同一凭据与审计；
控制台调它们，核心路由保持向后兼容。

## 角色

- `POST /api/v1/console/roles`：创建（只建 id 与名称；模型、记忆、文档另行配置）。
  响应只含 `id` 与 `name`——角色本身不带任何凭据，没有秘密需要一次性出示。
- `PUT /api/v1/roles/{id}`：改名。
- 核心 `POST /api/v1/roles` 仍保留，二者等价。

## 资产

- `POST /api/v1/console/assets`：注册资产。
  - `connection_type=agent`：响应**一次性返回** `agent_token`，此后任何 GET 都不再出现。
  - `connection_type=ssh`：只接受 `ssh_host / ssh_port / ssh_user / ssh_auth_kind /
    ssh_secret_ref / ssh_host_key`，**密钥内容不经过浏览器**——`ssh_secret_ref` 是指向
    服务器上秘密文件的指针。`ssh_host_key`（预置主机公钥）**必填**，缺失即 422，
    与连接器的 fail-closed 策略一致。
- `PUT /api/v1/assets/{id}/notes`：改名 / 备注 / 允许账号。
  - **接入方式与凭据不在这里改**：连接类型、SSH 密钥、agent token 轮换都仍走各自的
    管理员接口。这是刻意的——凭据变更不是随手可点的操作。
- 允许账号去重，重复即 422。

## 定制任务

- `POST /api/v1/console/custom-tasks`：创建触发/定时任务，响应**一次性返回**
  `trigger_token` 与 `trigger_path`。
- `PUT /api/v1/console/custom-tasks/{id}`：编辑。**不会重新返回触发令牌**
  （返回 `token_unchanged: true`）——令牌是凭据，只在创建时示一次。
- 定时任务的 cron 变更会重算 `next_fire_at`；禁用会取消 pending 投递；删除保留历史。

## 文档

- `PUT /api/v1/documents/{id}` / `DELETE`：控制台提供编辑框，保存有修订号，删除保留审计。
- 核心文档强制注入所有角色；非核心文档仅注入指定角色。

## Skills

Skill 是**本地、注入式的指令包**，是**数据不是代码**：

- 一张 `skills` 表：`id / name / content / role_ids / enabled / revision`。
- 本轮装配上下文时，把**本角色**的、**启用且未删除**的 Skill 文本作为
  `CURRENT_SKILLS` 注入系统消息（与文档并列）。
- **Skill 永远不能扩大权限**：它不能增加账号、改变模式或授予工具。它只是**可信度较低的
  参考材料**，和事件 payload、命令输出同等对待。
- 关闭（enabled=false）或删除即不再注入；删除保留审计记录。

API：`GET /api/v1/skills`、`PUT /api/v1/skills/{id}`、`DELETE /api/v1/skills/{id}`。

## 边界（不宣称的）

- 资产注册后**连接类型不可改**；SSH 密钥、agent token 轮换、角色模型配置**仍走管理员
  接口/CLI**，控制台只提供上述安全子集。
- 文档/Skill 的**并发编辑冲突检测**未做（后写覆盖，有修订号可对比）。
- 没有多用户/权限分级：控制台是**单管理员凭据**的运维面。
- Skill 不做沙箱/校验：它是文本，模型把它当参考读；**不要在其中写秘密或当作权限声明**。

## 测试

`tests/test_management.py`（12 项）：写接口均需管理员、角色改名与资产更新、
允许账号去重、**控制台创建角色（含 409 冲突）**、**控制台注册 agent 资产并保证 token
不出现在任何 GET**、**SSH 资产缺主机公钥即 422**、**定制任务创建返回令牌而编辑不返回**、
Skill 保存并注入本角色上下文、**其他角色的 Skill 不注入**、禁用/删除的 Skill 不注入、
文档保存与删除。
