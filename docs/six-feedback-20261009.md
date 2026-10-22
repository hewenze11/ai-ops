# 第三轮六项交付及集成阻断修复

## 纠错
上一版 `928efb7e8df281d3` 的“注销后保留卡片已完成”结论不成立：前端 fixture 虽有注销资产，GET /assets 仍强制过滤注销与停用本机。父会话独立复核发现缺失，现已补齐，不能将旧版DOM通过当全栈验收。

本次逐项核对 `scratch/ai-ops-development/six-feedback-integration-handoff.md`：
1. GET /api/v1/assets 支持 include_retired、include_disabled_local，默认均false保留旧API语义；增加retired_at、local_enabled（非本机为null）。资产卡刷新和心跳资产筛选均显式传两个true。注销仍保留卡，停用本机仍能发现设置恢复；purge才移除。
2. Key明确清除按钮，确认后待保存，保存同时关闭角色模型，不同时发送新Key；取消清除不自动重启模型。
3. 旧告警全局继承测试显式notification:null，新建任务默认关闭；旧心跳测试文案已同步。
4. /logs/heartbeats为24小时连续表分页、可信TTL断联标注、未知保留；all默认排除心跳。没有创建心跳知识库文档。
5. 独立Provider、角色模型归档、不限步数、凭据旁车备份详见model-turns.md、backup.md；全量安装包复测完成。

## API验收证据
新增 tests/test_asset_inventory_visibility.py 使用真实FastAPI路由和隔离SQLite，验证：注销后默认列表隐藏、管理列表保留；停用本机默认隐藏、管理列表含local_enabled=false；两参数分别有效；未鉴权401、无效参数422；编辑写真源；恢复后默认列表重新可见，派生核心文档同步；错误ID不能purge、正确purge移除；注销/恢复/删除审计均存在。既有claimed/unknown安全测试保留通过。
生产环境仅只读验证，不注销/停用用户资产作为试验；安装镜像内完整回归包含上述真实API集成测试。

## 最新部署（替代上一版）
- 公网入口：http://172.236.254.94:8765/
- 镜像：ai-ops-service:ui-20261009t054157z
- UI指纹：0a5554c22c3c1cc0
- JS SHA256：15b7a0147cd24df80aa09cbb636473cd62086c427b0c4202eabb9d4173f52845
- CSS SHA256：1787501d836a5dbe693baf3215270496737935dfbaa338e1f973d89fda8c0db3
- 首页与资源no-store，公网字节与本地源码一致；37生产包文件哈希一致。
- 本地Python348通过、1平台跳过；镜像安装包Python349通过；Node DOM通过。
- 停服务一致性备份：/opt/ai-ops/backups/pre-ui-20261009T054157Z.tar.gz（0600）
- Compose备份：/opt/ai-ops/backups/compose-pre-ui-20261009T054157Z.yaml
- 回滚镜像：ai-ops-service:pre-ui-20261009t054157z（此为上版，不含本次列表修复）；回滚前检查claimed，无需回写用户业务库。
- 部署前后13张配置表与admin/model文件相同；另逐表验证role_turns/tasks/model_calls完全相同，未启用模型或补跑历史队列。通知worker仍关闭。实际GET OpenAPI包含两个boolean参数默认false，管理列表返回retired_at/local_enabled。
- 未做浏览器验收，用户自行体验；未操作.239/.19，未push，未做真实付费模型调用、真实Webhook或资产命令。

证据：工作区scratch/ai-ops-development/frontend-release-20261009/manifest.json及verification.json。
