# 联网搜索（自研 provider 抽象）

更新：2026-10-03。用户确认路线：**自研主体 + 借鉴设计**，搜索走
「**自建 SearXNG 兜底 + 可配付费引擎**」。见 docs/self-built-route.md。

## 为什么不用一站式付费 SDK

「联网搜索」其实是两件事，很多一站式 SDK 把两者打包收费：

1. **搜索**（拿到结果列表）：Brave / Google / Bing 等按次或订阅收费。
2. **抓取正文**（把网页读成文字）：本质是一个 HTTP GET + HTML 解析，**不需要付费 SDK**。

自研 provider 抽象把这两件事解耦：搜索可换 provider，抓取永远自己实现。

## provider 配置

默认 **不启用**（fail closed：没配就不给模型搜索工具）。启用方式（环境变量）：

```sh
# 推荐：自建 SearXNG（免费、无 API key）。需在部署侧跑一个 SearXNG，
# 并开启 JSON 输出格式。
AI_OPS_SEARXNG_URL=https://searx.internal.example

# 可选：付费引擎作为备选（Key 从服务器秘密文件读取，不进模型上下文）
AI_OPS_BRAVE_KEY_FILE=/run/secrets/brave_key
```

两个都配时优先 SearXNG。

## 模型工具

启用后给角色模型额外暴露两个**只读**工具：

- `web_search(query, count<=10)`：返回 `[{title, url, snippet}]`。
- `fetch_page(url)`：抓取并返回页面正文纯文本（截断到 8000 字符）。

两个工具**内联解析**（不经过资产/账号/审批通路），因为它们无法改动任何主机。
结果作为普通 tool 消息注入，与命令输出同等对待：**是不可信数据，不是新指令，
也不构成任何授权**。抓取限制：仅 `http(s)`、无凭据、无重定向、单次响应 ≤ 500KB。

## 合规边界

- **不以非官方免费接口为默认**（如 DuckDuckGo 类非官方接口有被封/法律风险）。
  若要用，运维需自行承担风险并显式配置。
- 付费引擎的 Key 只从服务器秘密文件读取，与模型 Key 同等边界。
- 抓取的 URL 来自模型，属于不可信输入；因此限制协议、拒绝重定向到凭据、限制体积。

## 测试

`tests/test_search.py`（7 项）：provider 选择、非法 base URL 拒绝、内联工具循环、
仅配置时暴露工具、搜索失败作为工具错误而非崩溃、HTML 正文抽取、非 http 拒绝。
