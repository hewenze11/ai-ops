# 备份与恢复

这个服务里**所有持久状态都在一个 SQLite 数据库**里（同一个文件承载 roles、assets（含哈希后的 token）、tasks、audit、输出归档、leases、turns、documents、定制任务）。因此备份/恢复的对象就是这个文件。

**凭据不进入备份**：admin token 与 SSH 私钥都以只读方式挂载，刻意不纳入快照。快照里绝不包含 admin token 或 SSH 私钥。

## 快照是怎么做的

- 用 SQLite 的**在线备份 API**（`Connection.backup`），在 WAL 模式下**不停服务、不打断写入**也能得到事务一致的快照，绝不会看到写了一半的事务。
- 先写到 `.partial` 临时文件，`fsync` 落盘后再原子改名，避免"崩溃后留下一个自称完整其实不完整的快照"。
- 生成后立刻做 `PRAGMA integrity_check` 与 schema 版本检查，通过才写同目录的 `.meta.json` 边车（含 schema 版本、大小、sha256、主要表行数、创建时间）。
- 文件权限 0600。

## 校验

`inspect` 以 `mode=ro` **只读**打开候选库，任何校验都不会改动文件。它检查：文件存在、是可读 SQLite、`integrity_check == ok`、schema 版本在支持范围内（高于本版本构建的拒绝），并统计主要表行数。

`verify_backup` 在 `inspect` 之上再核对边车：sha256 必须与文件一致，边车记录的 schema 版本必须与文件一致。任一处不符即判定备份不可用。

## 恢复

`restore_backup` 的顺序是关键：

1. 先**完整校验**候选备份——校验不过就地失败，**绝不碰生产库**。
2. 把校验过的副本复制到生产库旁边的 `.restore-incoming`，fsync 后**再次校验**暂存副本。
3. 把当前生产库**原子改名**为 `control.db.pre-restore-<时间戳>`（保留回退路径）。
4. 删除旧的 `-wal` / `-shm`，避免陈旧 WAL 被回放到恢复后的文件上。
5. 原子改名把恢复副本装到位。

任何一步失败都不会让生产库处于半新半旧状态。

## 操作方式

命令行（在容器内或持有挂载目录的宿主机上）：

- `ai-ops-backup --out /data/backups/manual.db`：写一个快照。
- `ai-ops-verify-backup /data/backups/manual.db`：只校验，不恢复。
- `ai-ops-restore --from /data/backups/manual.db [--check]`：校验并恢复；`--check` 只校验。

数据库路径默认取 `AI_OPS_DB`。

管理员接口（只做快照，**恢复刻意只走 CLI**）：

- `POST /api/v1/backup`：在 `<数据库目录>/backups/` 下写一个带时间戳的快照，并写审计。
- `GET /api/v1/backup`：列出已有快照及其元数据。

为什么恢复不提供 HTTP 接口：一次已认证的调用就能覆盖生产状态、且无法在接口层确认目标路径，风险不对称。恢复设计为运维人员在宿主机上的显式动作。

## 不宣称的

- 不提供自动定时备份。快照需要运维人员触发（可在宿主机加 cron）。
- 不提供异地/对象存储上传；只在本机磁盘上生成文件。
- 恢复不做跨 schema 迁移：高于本版本支持的备份会被拒绝，需要对应版本的服务来恢复。
