# Agent 常驻安装、升级与卸载

当前推荐的 Linux 执行端方式是独立 venv + systemd 常驻轮询：不把宿主机用户执行放进特权容器；容器仍可作为容器内隔离测试用途，不能借此管理宿主机账号。

## 安装

```sh
python3 -m venv /opt/ai-ops-agent/venv
/opt/ai-ops-agent/venv/bin/pip install ./ai-ops-agent-0.1.0.dev2-py3-none-any.whl
install -d -m 700 /etc/ai-ops-agent
/opt/ai-ops-agent/venv/bin/ai-ops-agent install-systemd \
  --server-url https://control.example \
  --asset-id your-asset \
  --token REPLACE_FROM_SECURE_PROVISIONING \
  --user ops_read
```

`install-systemd` 会：

1. 生成 `/etc/ai-ops-agent/config.json`，权限 0600，属主为服务运行账号；
2. 写入 `/etc/systemd/system/ai-ops-agent.service`；
3. 执行 `systemctl daemon-reload` 并按需要 `enable --now`。

Token 通过主服务 `POST /api/v1/assets` 一次性取得：它只写入本机 0600 文件，不进入命令行历史、Git 或聊天。多个 `--user` 可重复传入。想要以 root 启动 Agent 才能切换到其他系统账号；此时允许 root 就等于把机器交给该角色，这是明确剩余风险。命令可加 `--print-only` 先看将生成的文件内容，`--no-start` 只写文件不启动。

## 升级

```sh
/opt/ai-ops-agent/venv/bin/pip install --upgrade ./ai-ops-agent-<new>.whl
systemctl restart ai-ops-agent
```

- 配置格式未变时不需重新注册。
- 升级前 journal 里 `started` 的记录按未知处理，不会重放命令。
- 协议升级后（1.0 → 1.1）旧 Agent 仍能被主服务调度，但不会获得运行中取消能力，直到完成升级。

## 卸载

```sh
systemctl disable --now ai-ops-agent
rm /etc/systemd/system/ai-ops-agent.service
systemctl daemon-reload
```

配置、journal 与 venv 目录需要自行确认后删除；`install-systemd` 不做隐式清理。已在主服务注册的资产需单独处理（当前预览未提供删除接口，属于已知缺口）。

## 运行与排障

```sh
systemctl status ai-ops-agent
journalctl -u ai-ops-agent -n 50
```

日志只包含异常类型，不含响应正文、请求头、配置或秘密。`--once` 仍是联调入口（处理待回传结果并尝试领取一个任务）。主服务上线前请先确认 `GET /api/v1/agents/{asset_id}/status` 显示在线。

## 未知执行处置

Agent 崩溃重启后，未确认的任务上报 `unknown`，主服务阻塞该角色后续任务。当前预览没有一键处置接口；应先人工核实机器实际状态，再决定是否通过数据库/后续管理接口解除。不要用取消来清除 unknown。
