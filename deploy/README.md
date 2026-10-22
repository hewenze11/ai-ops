# 安装入口与历史脚本说明

2026-10-14更新。

## 当前入口

**`install.sh`（本目录）是当前推荐入口**：curl 一行完成环境检查、拉取公开镜像 ghcr.io/hewenze11/ai-ops、创建专属目录、生成配置与管理员 Token 并启动。

```bash
curl -fsSL https://github.com/hewenze11/ai-ops/releases/latest/download/install.sh | bash
```

设计原则：不检查“目录/文件是否已存在”并停下来追问；只需按需创建，已存在的配置/凭据/compose 文件保留不覆盖。教程与目录说明见官网仓 `docs/DEPLOY-CONTROL.md`。

官网仓还提供一个Python安装器：

- `../ai-ops-web/deploy/install.py`（从本仓根目录计算）
- `../ai-ops-web/deploy/INSTALLER.md`

该Python文件可独立分发，分别支持control与**容器Agent**。默认只打印计划；显式apply才安装。官网两仓自身的Compose与此运维产品安装器是两套系统。

## 历史脚本：保留，但不再作为当前推荐入口

本目录存在`install-control.sh`和`install-agent.sh`，属于早期预览流程，当前**不应直接执行**：自动安装系统依赖/Docker、默认改变宿主账号与sudo权限、原生venv/systemd安装、含重复执行覆盖配置等需另行修复的行为。容器包权限与源码公开状态独立；不要把新入口当作宿主原生安装器。

手动部署见本仓`docs/docker-manual.md`与Agent仓`docs/docker-manual.md`。
