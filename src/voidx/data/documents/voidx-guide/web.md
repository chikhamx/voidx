## Web 搜索

| 命令 | 作用 |
|------|------|
| `/tavily set` | 设置 Tavily API key |
| `/tavily show` | 查看 key 状态 |
| `/tavily delete` | 删除 key |
| `/bocha set` | 设置 Bocha API key |
| `/bocha show` | 查看 key 状态 |
| `/bocha delete` | 删除 key |

搜索后端按优先级分层并发：有 key 时 Tavily 与 Bocha 并发，失败后由 DuckDuckGo 与 Bing（`cn.bing.com`）并发兜底。Bocha 可通过 `BOCHA_API_KEY` 环境变量配置，也可使用 `/bocha set` 保存到设置。Bing 和 DuckDuckGo 不需要 API key。

配置后 agent 可自动进行网页搜索和抓取。

---
