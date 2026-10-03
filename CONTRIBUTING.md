# 贡献指南

感谢你想参与 AI Ops。这是一个**预览阶段**的运维控制服务与执行协议实现，接口和数据结构仍会变动。

## 开始之前

- 先读 [README.md](README.md) 与 `docs/` 下的设计文档，尤其是执行协议（`docs/protocol-v11.md`）与安全边界（README 的「安全与恢复边界」）。
- 本项目目前**主要由项目所有者驱动**：欢迎 issue 讨论，较大改动请先开 issue 对齐方向，再提 PR，避免白做。
- 这是一个运维产品，**安全性优先于便利性**：任何"图省事"的改动（放宽权限、隐式清理、跳过确认）默认不接受，除非有明确论证。

## 开发环境

```sh
python -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install '.[test]'
pytest -q
```

主服务与执行端（`hewenze11/ai-ops-agent`）是两个仓库。改协议相关逻辑时，两边要一起考虑兼容性。

## 提交规范

- 提交信息用**祈使句**、说明"做了什么/为什么"，例如 `Fix agent so a settled result cannot wedge the worker`。
- 一个 PR 聚焦一件事，避免把重构和功能混在一起。
- 新行为要有测试；破坏性变更要在 commit 与 PR 描述里写清楚。

## 安全红线（PR 会被直接拒绝的情况）

- 在代码、测试、文档、CI 里硬编码任何**真实密钥/token**（`sk-`、`ghp_`、私钥、admin token 等）。仓库发布前有密钥扫描，命中即阻断。
- 降低执行端隔离：例如去掉最小权限账号、给只读账号写权限、把 agent 变成 root 全权执行。
- 让"未知执行"能被静默绕过（自动重跑、靠取消解除等）。
- 把秘密写进命令行参数、Git、或模型上下文。

## 报告问题

- 普通 bug / 功能建议：用 issue 模板。
- **安全漏洞：不要开公开 issue**，见 [SECURITY.md](SECURITY.md)。

## 许可证

本项目**尚未确定开源许可证**（见 README 末尾）。在许可证确定前，贡献默认按"项目所有者可自由使用/再许可"理解；提交 PR 即表示你同意这一点。许可证确定后会更新本节。
