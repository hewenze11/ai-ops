# 自研主题 + 借鉴 OpenClaw 设计（P2 路线决策）

更新：2026-10-03。用户已确认：**不 fork OpenClaw，产品主体纯自研**；借鉴 OpenClaw
的**设计思路**（渠道抽象、上下文引擎、凭据边界、可插拔 provider），不复制其全家桶体积。

## 决策

- **主体自研**：ai-ops 主服务继续用 Python/FastAPI/SQLite，Linux Agent 纯标准库。
  保持「轻量、皮实、可控」。不引入 Node Gateway、不引入 30+ 渠道、不做桌面/移动端。
- **借鉴清单**（择优实现，不抄代码）：
  1. **渠道抽象**：把「Web / 飞书 / 微信」统一成「一条输入进入角色轮次」的接口。
     难度在于跨入口共享同一角色记忆——由主服务持有角色记忆，渠道只做投递。
  2. **上下文引擎可插拔**：把上下文组装抽成独立模块（当前 `turns.py` 里内联的
     `request_body` 拆出），便于以后替换/测试。
  3. **凭据边界**：沿用「生效中才校验 + 运行时快照」，已在轮换/秘密文件中体现。
  4. **可插拔 provider**：搜索、模型都抽象成 provider 接口。

## 搜索能力设计（用户指定）

**自建 SearXNG 兜底 + 可配付费引擎**，做成可插拔 provider：

- `search_providers` 抽象：统一的 `search(query, count) -> [{title,url,snippet}]`。
- 默认 provider：**自建 SearXNG**（`SEARXNG_BASE_URL`，JSON 输出，免费无 API key）。
- 可选 provider：Brave / Bing / Google CSE（需要 API key，走秘密文件）。
- 抓取正文：`fetch_page(url)`，直接 HTTP 取 HTML→纯文本，不需要付费 SDK。
- 合规提醒：非官方免费接口（DuckDuckGo 类）不作为默认；文档注明风险。

## 分步实施

1. 把 `turns.py` 的上下文组装抽出为 `context.py`（可插拔，行为不变，先补测试）。
2. 新增 `search.py`：provider 抽象 + SearXNG provider + 可选付费 provider + 抓取；
   新增 `web_search` 模型工具（受同一 tool 授权边界约束：只读、无凭据、可在本轮禁用）。
3. 搜索工具默认**只读**、结果作为工具输出注入，与 execute_command 同等对待（不可信数据）。
4. 文档：`docs/search.md`，说明 provider 取舍与合规边界。
5. 按天分层记忆（全文/压缩/梗概 + 旧记忆按需加载）作为 P2 后续项，独立设计。

## 不做

- 不 fork/内嵌 OpenClaw 网关。
- 不做渠道全家桶；飞书/微信接入在 P3 用最简方式实现（WebHook + 主服务持有记忆）。
- 不做非官方付费绕过的搜索接口。
