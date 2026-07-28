# Websearch 降级链改进方案

> **Status: Done** — Archived on 2026-07-25.

## 目标

在 `websearch` 工具中加入 Bocha 搜索后端和 Bing 爬虫兜底，实现 `Tavily → Bocha → DuckDuckGo → Bing` 四级降级链。每个后端"有配置就试，失败就降级"，不配置 Bocha 的用户行为不变，Bing 爬虫作为最终兜底覆盖 DuckDuckGo 不可达的场景。

**国内无外网场景**：Tavily 和 DuckDuckGo 在国内直连不可达（DuckDuckGo 自 2014 年起被墙），Bocha（博查AI，国内服务）和 Bing（`cn.bing.com`，微软国内服务器）可直连。降级链保证：无外网时 Tavily/DDG 自动失败降级，最终由 Bocha 或 Bing 兜底，不会卡死。

## 现状

当前 `src/voidx/tools/web/search.py` 的 `execute()` 流程：

1. MCP 路由（如已配置 `backend="mcp"`，直接返回，不走降级链）
2. Tavily（有 Key 才试，失败记录到 `fallback_errors`）
3. DuckDuckGo（兜底，无 Key，HTML 爬取）

缓存 key 的 `backend` 字段只标记 `"tavily"` 或 `"duckduckgo"`，不区分 Bocha。

## 改进方案

### 并发查询 + 优先级聚合

**问题**：串行降级链在国内无外网场景下延迟过高。Tavily 和 DuckDuckGo 都被墙，需等两次超时（15s × 2 = 30s）才到 Bing 兜底。

**策略**：分层并发，组内并发查询，组间串行降级。高优先级组任一成功就返回，避免等待低优先级组。

```
MCP 路由（最高优先级，全有全无）

  ┌─ 高优先级组（API 后端，有 Key 才加入）─────────────┐
  │  Tavily + Bocha  并发查询                          │
  │  任一成功 → 按优先级聚合结果，立即返回             │
  │  全部失败/超时 → 降级到低优先级组                  │
  └────────────────────────────────────────────────────┘

  ┌─ 低优先级组（无 Key 爬虫，始终加入）───────────────┐
  │  DuckDuckGo + Bing  并发查询                       │
  │  任一成功 → 按优先级聚合结果，立即返回             │
  │  全部失败 → 返回错误聚合信息                       │
  └────────────────────────────────────────────────────┘
```

**为什么分两组而非全部并发**：
- API 后端（Tavily/Bocha）质量高于爬虫（DDG/Bing），优先返回 API 结果
- 如果 API 后端成功，不需要浪费爬虫请求
- 全部并发会让每次搜索都打 4 个后端，增加不必要的负载

**聚合规则**：
1. 同组内多个后端都返回结果时，按优先级合并：高优先级结果排前面，低优先级补充
2. URL 去重：`normalize_search_results()` 已有 URL 去重，跨后端重复结果自动去除
3. `backend` 元数据标记所有实际命中的后端（如 `"tavily+bocha"`）

**延迟对比**：

| 场景 | 串行降级 | 并发策略 |
|---|---|---|
| 有外网 + Tavily Key | ~2s（Tavily 命中） | ~2s（Tavily 命中） |
| 有外网 + 无 Key | ~2s（DDG 命中） | ~2s（DDG/Bing 并发，DDG 先到） |
| 无外网 + Bocha Key | ~17s（Tavily 超时 → Bocha） | ~3s（Tavily+Bocha 并发，Bocha 先到） |
| 无外网 + 无 Key | ~30s（Tavily+DDG 双超时 → Bing） | ~15s（DDG+Bing 并发，Bing 先到） |

**超时设计**：
- 每个后端超时保持 15s 不变
- 并发查询用 `asyncio.wait_for` 包裹 `asyncio.gather`，组级超时 = 15s（不是各后端超时之和）
- 组内所有后端共享 15s 超时窗口，先到先得

**国内无外网场景的优化效果**：
- 无外网 + 无 Bocha Key：从 30s 降到 ~15s（DDG+Bing 并发，Bing 命中）
- 无外网 + 有 Bocha Key：从 17s 降到 ~3s（Tavily+Bocha 并发，Bocha 命中）

### Bing 爬虫兜底

参考 `multi_search.py` 中的 `BingScraper` 设计，在 DuckDuckGo 之后加一个 Bing HTML 爬虫作为最终兜底。

**为什么需要**：DuckDuckGo 在国内被墙（2014 年起），直连不可达。Bing 在国内可直连（`cn.bing.com` 有国内服务器），作为最终兜底覆盖国内无外网场景。实测对比：

| 维度 | DuckDuckGo | Bing (cn.bing.com) |
|---|---|---|
| 国内可达性 | ❌ 被墙 | ✅ 可直连 |
| 英文搜索质量 | **高**（10 条，相关性好） | 低（5 条，相关性差） |
| 中文搜索质量 | 高（12 条） | 高（5 条，snippet 更完整） |
| 结果数 | 10-12 | 5（移动端 `count` 参数无效） |

结论：有外网时 DuckDuckGo 更好（排在前面），无外网时 DuckDuckGo 必然失败、Bing 兜底。降级链顺序 `DDG → Bing` 在两种场景下都是最优的。

**实现方式**（和 DuckDuckGo 一致，用标准库 `HTMLParser`，不引入 `beautifulsoup4`）：

- 请求 `https://cn.bing.com/search?q={query}`，使用移动端 UA 绕过 captcha
- 解析 `<li class="b_algo">` 结果块
- 从每个块中提取：第一个 `<a href>` 的 URL 和标题文本，`<p>` 标签的 snippet
- 域名过滤用 `matches_domain()`（和 DuckDuckGo/Bocha 一致）

**Bing 搜索结果 HTML 结构**（实测）：

```html
<li class="b_algo">
  <h2><a href="https://example.com">标题</a></h2>
  <div class="b_caption">
    <p>摘要文本</p>
  </div>
</li>
```

**反爬注意事项**：
- 桌面端 UA 会被 captcha 拦截（实测返回 captcha 页面）
- 移动端 UA 可正常获取结果（实测 5 个 `b_algo` 块）
- 请求头需包含 `Accept-Language`，否则可能返回不同结构
- `www.bing.com` 中文查询返回空，必须用 `cn.bing.com`

### Bocha API 接口

来自官方页面 <https://open.bochaai.com>。

**请求：**

```http
POST https://api.bochaai.com/v1/web-search
Authorization: Bearer <BOCHA_API_KEY>
Content-Type: application/json

{
  "query": "搜索关键词",
  "freshness": "oneYear",
  "summary": true,
  "count": 8
}
```

**响应：**

```json
{
  "webPages": {
    "totalEstimatedMatches": 606721,
    "value": [
      {
        "name": "标题",
        "url": "https://example.com",
        "snippet": "摘要",
        "summary": "更长摘要",
        "siteName": "站点名",
        "datePublished": "2024-07-22T00:00:00+08:00"
      }
    ]
  }
}
```

**映射到统一格式：**

| Bocha 字段 | 统一格式字段 |
|---|---|
| `url` | `url` |
| `name` | `title` |
| `snippet`（空则用 `summary`） | `snippet` |

**域名过滤：** Bocha API 不支持 `include_domains`/`exclude_domains` 参数，在客户端用 `matches_domain()` 做过滤（和 DuckDuckGo 一致）。

## 改动文件

### 1. `src/voidx/tools/web/search.py`

**新增 `_search_bocha()` 函数**（约 30 行，和 `_search_tavily()` / `_search_duckduckgo()` 同级）：

```python
async def _search_bocha(
    query: str,
    api_key: str,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
    max_results: int = 10,
) -> list[dict[str, str]]:
    """Search via Bocha API. Returns list of {url, title, snippet}."""
    url = "https://api.bochaai.com/v1/web-search"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "query": query,
        "count": max_results,
        "summary": True,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("webPages", {}).get("value", []):
        snippet = item.get("snippet", "") or item.get("summary", "")
        results.append({
            "url": item.get("url", ""),
            "title": item.get("name", ""),
            "snippet": snippet,
        })

    if allowed_domains:
        results = [r for r in results if any(matches_domain(r["url"], d) for d in allowed_domains)]
    if blocked_domains:
        results = [r for r in results if not any(matches_domain(r["url"], d) for d in blocked_domains)]

    return results[:max_results]
```

**新增 `_BingResultParser` 和 `_search_bing()` 函数**（和 `_DDGResultParser` / `_search_duckduckgo()` 同级）：

```python
class _BingResultParser(HTMLParser):
    """Extract search results from Bing HTML page (b_algo blocks)."""

    def __init__(self):
        super().__init__()
        self._results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._capture = ""
        self._in_title_link = False
        self._in_snippet = False
        self._in_b_algo = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attr = dict(attrs)
        cls = attr.get("class", "")

        if tag == "li" and "b_algo" in cls:
            self._in_b_algo = True
            self._current = {"url": "", "title": "", "snippet": ""}
            return

        if not self._in_b_algo:
            return

        if tag == "a" and self._current and not self._current["url"]:
            href = attr.get("href", "")
            if href.startswith("http"):
                self._current["url"] = href
                self._in_title_link = True
                self._capture = ""

        if tag == "p" and self._current:
            self._in_snippet = True
            self._capture = ""

    def handle_endtag(self, tag: str):
        if tag == "a" and self._in_title_link:
            if self._current is not None:
                self._current["title"] = self._capture.strip()
            self._in_title_link = False

        if tag == "p" and self._in_snippet:
            if self._current is not None:
                self._current["snippet"] = self._capture.strip()
            self._in_snippet = False

        if tag == "li" and self._in_b_algo:
            if self._current and self._current["url"]:
                self._results.append(self._current)
            self._current = None
            self._in_b_algo = False

    def handle_data(self, data: str):
        if self._in_title_link or self._in_snippet:
            self._capture += data

    def results(self) -> list[dict[str, str]]:
        return self._results


async def _search_bing(
    query: str,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
    max_results: int = 10,
) -> list[dict[str, str]]:
    """Search via Bing HTML scraping (mobile UA to bypass captcha)."""
    from urllib.parse import quote

    search_url = f"https://cn.bing.com/search?q={quote(query)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                      "Version/17.2 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(search_url, headers=headers)
        resp.raise_for_status()

    parser = _BingResultParser()
    try:
        parser.feed(resp.text)
    except Exception as exc:
        log_tool_event("websearch_parse_failed", tool_name="websearch", message=f"Bing HTML parse failed: {exc}")
    results = parser.results()

    if allowed_domains:
        results = [r for r in results if any(matches_domain(r["url"], d) for d in allowed_domains)]
    if blocked_domains:
        results = [r for r in results if not any(matches_domain(r["url"], d) for d in blocked_domains)]

    return results[:max_results]
```

**重写 `execute()` 方法**为分层并发查询 + 优先级聚合：

```python
async def execute(self, inp: WebSearchInput) -> ToolResult:
    query = inp.query
    fallback_errors: list[str] = []

    # 1. MCP 路由（最高优先级，全有全无，逻辑不变）
    mcp_result = await self._try_mcp_route(inp)
    if mcp_result is not None:
        return mcp_result

    # 2. 计算缓存 key（backend 字段标记所有可能命中的后端）
    tavily_key = self._get_tavily_key()
    bocha_key = self._get_bocha_key()
    backends = []
    if tavily_key:
        backends.append("tavily")
    if bocha_key:
        backends.append("bocha")
    backends.append("duckduckgo")
    backends.append("bing")
    cache_key = search_cache_key(
        query=query,
        allowed_domains=inp.allowed_domains,
        blocked_domains=inp.blocked_domains,
        max_results=inp.max_results,
        backend="+".join(backends),
    )
    cached = WEB_TOOL_CACHE.get(cache_key)
    if cached is not None:
        return cached

    # 3. 构建高优先级组（API 后端，有 Key 才加入）
    high_priority: list[tuple[str, Awaitable]] = []
    if tavily_key:
        high_priority.append(("tavily", _search_tavily(
            query, tavily_key, inp.allowed_domains, inp.blocked_domains, inp.max_results,
        )))
    if bocha_key:
        high_priority.append(("bocha", _search_bocha(
            query, bocha_key, inp.allowed_domains, inp.blocked_domains, inp.max_results,
        )))

    # 4. 高优先级组并发查询，任一成功就返回
    if high_priority:
        results = await self._run_group_concurrent(high_priority, fallback_errors)
        if results:
            backend = "+".join(name for name, _ in high_priority if any(r.get("_backend") == name for r in results))
            result = self._format_results(query, results, backend, fallback_errors)
            WEB_TOOL_CACHE.set(cache_key, result, ttl_seconds=600)
            return result

    # 5. 高优先级组全部失败，降级到低优先级组（无 Key 爬虫，始终加入）
    low_priority: list[tuple[str, Awaitable]] = [
        ("duckduckgo", _search_duckduckgo(
            query, inp.allowed_domains, inp.blocked_domains, inp.max_results,
        )),
        ("bing", _search_bing(
            query, inp.allowed_domains, inp.blocked_domains, inp.max_results,
        )),
    ]
    results = await self._run_group_concurrent(low_priority, fallback_errors)
    if results:
        backend = "+".join(name for name, _ in low_priority if any(r.get("_backend") == name for r in results))
        result = self._format_results(query, results, backend, fallback_errors)
        WEB_TOOL_CACHE.set(cache_key, result, ttl_seconds=600)
        return result

    # 6. 所有后端都失败了
    return ToolResult(
        output=f"Search failed for: {query}. Errors: {'; '.join(fallback_errors)}",
        metadata={"query": query, "results": 0, "fallback_errors": fallback_errors},
    )
```

**新增 `_run_group_concurrent()` 辅助方法**——组内并发查询 + 优先级聚合：

```python
async def _run_group_concurrent(
    self,
    backends: list[tuple[str, Awaitable[list[dict]]]],
    fallback_errors: list[str],
) -> list[dict]:
    """Run a group of backends concurrently, aggregate results by priority.

    Each backend's results are tagged with _backend for later aggregation.
    Returns empty list if all backends fail.
    """
    async def _safe_run(name: str, coro: Awaitable) -> tuple[str, list[dict]]:
        try:
            results = await coro
            for r in results:
                r["_backend"] = name
            return name, results
        except Exception as exc:
            fallback_errors.append(f"{name}: {exc}")
            return name, []

    # 并发查询所有后端，组级超时 15s
    try:
        gathered = await asyncio.wait_for(
            asyncio.gather(*[_safe_run(name, coro) for name, coro in backends]),
            timeout=15,
        )
    except asyncio.TimeoutError:
        for name, _ in backends:
            fallback_errors.append(f"{name}: timeout")
        return []

    # 按优先级聚合：高优先级结果排前面，低优先级补充
    all_results: list[dict] = []
    for name, results in gathered:  # gathered 保持 backends 顺序
        all_results.extend(results)

    # URL 去重 + rank 重排（normalize_search_results 处理去重）
    return all_results
```

**聚合说明**：
- `_safe_run` 给每个结果打上 `_backend` 标签，聚合后 `_format_results` 可据此计算 `backend` 元数据
- `asyncio.gather` 保持输入顺序，所以 `gathered` 的顺序就是优先级顺序
- `normalize_search_results()` 在 `_format_results` 内部调用，自动处理跨后端 URL 去重
- `_backend` 标签是临时字段，`_format_results` 输出前需清除（或 normalize 时忽略）

**超时行为**：
- `asyncio.wait_for(gather(...), timeout=15)` 让组内所有后端共享 15s 超时窗口
- 先到的结果不会被阻塞等待慢的后端——但 `gather` 会等所有完成
- 如果需要"任一成功立即返回"的语义，改用 `asyncio.as_completed` + 首个成功即返回（见下方变体）

**变体：任一成功立即返回**（更激进，但实现更复杂）：

```python
async def _run_group_first_success(
    self,
    backends: list[tuple[str, Awaitable[list[dict]]]],
    fallback_errors: list[str],
) -> list[dict]:
    """Run backends concurrently, return first successful result set."""
    tasks = {
        asyncio.ensure_future(coro): name
        for name, coro in backends
    }
    try:
        # 用 as_completed 实现首个成功即返回
        for coro in asyncio.as_completed(tasks.keys(), timeout=15):
            try:
                results = await coro
                if results:
                    name = tasks.get(coro, "unknown")
                    for r in results:
                        r["_backend"] = name
                    # 取消其他任务
                    for t in tasks:
                        t.cancel()
                    return results
            except Exception as exc:
                fallback_errors.append(str(exc))
    except asyncio.TimeoutError:
        for name, _ in backends:
            fallback_errors.append(f"{name}: timeout")
    return []
```

**选择建议**：
- 默认用 `_run_group_concurrent`（gather 全部完成再聚合）：实现简单，能聚合多后端结果，质量更高
- 如果追求极致延迟（任一成功就返回），用 `_run_group_first_success`：但只返回单后端结果，丢失聚合优势
- **推荐 `_run_group_concurrent`**：组内并发已经把延迟压到单后端超时（15s），聚合多后端结果质量更高

**缓存 key 计算**：`backend` 字段标记所有可能命中的后端（如 `"tavily+bocha+duckduckgo+bing"`），确保不同配置组合不会命中同一缓存。计算逻辑见 `execute()` 代码示例中的步骤 2——根据 `tavily_key`/`bocha_key` 是否存在构建 backends 列表，用 `"+".join(backends)` 作为 `backend` 参数传入 `search_cache_key()`。

**新增 `_get_bocha_key()` 方法**（和 `_get_tavily_key()` 同级）：

```python
def _get_bocha_key(self) -> str | None:
    env_key = os.environ.get("BOCHA_API_KEY")
    if env_key:
        return env_key
    if self._settings:
        return self._settings.get_bocha_api_key()
    return None
```

### 2. `src/voidx/config/settings_api_keys.py`

新增三个方法（仿照 Tavily）：

```python
# ── bocha API key ──────────────────────────────────────────────────────

def get_bocha_api_key(self) -> str | None:
    """Get Bocha API key. Env var BOCHA_API_KEY takes priority over config file."""
    import os
    env_key = os.environ.get("BOCHA_API_KEY")
    if env_key:
        return env_key
    return self._effective_data().get("bocha_api_key") or None

def set_bocha_api_key(self, api_key: str | None) -> None:
    self._set_setting("bocha_api_key", api_key)

def delete_bocha_api_key(self) -> None:
    self._pop_setting("bocha_api_key")
```

### 3. `src/voidx/config/settings.py`

`GLOBAL_KEYS` 加入 `"bocha_api_key"`：

```python
GLOBAL_KEYS = frozenset({
    "mcpServers",
    "tavily_api_key",
    "bocha_api_key",   # 新增
    "codeIde",
    "userProfile",
    "web",
    "update_check",
    "parallel_subagents",
    "retry",
})
```

### 4. `src/voidx/agent/slash/handler.py`

新增 `/bocha` 命令（仿照 `/tavily`，但更简单——Bocha 不自动配置 MCP 路由，只管理 API Key）：

```python
# 命令注册（约 line 296）：
"/bocha": lambda: self._bocha(args),

# 处理函数（约 line 672 之后）：
async def _bocha(self, args: str) -> None:
    """Configure Bocha API key for web search."""
    settings = self.host.settings
    if not settings:
        ui.error("No settings available.")
        return

    if not args or args.strip() == "show":
        key = settings.get_bocha_api_key()
        if key:
            ui.print(f"Bocha API key: [cyan]{self._mask_key(key)}[/cyan]")
        else:
            ui.print("[dim]Bocha API key not configured.[/dim]")
        ui.print("[dim]Usage: /bocha set | /bocha delete[/dim]")
        return

    parts = args.split(None, 1)
    action = parts[0].strip().lower() if parts else ""
    if action == "set":
        if len(parts) > 1 and parts[1].strip():
            ui.error("Do not include the API key in command text. Use /bocha set.")
            return
        api_key = await self._prompt("Bocha API key", default="", secret=True)
        if api_key is None:
            ui.print("[dim]Cancelled.[/dim]")
            return
        api_key = api_key.strip()
        if not api_key:
            ui.error("Bocha API key is required.")
            return
        settings.set_bocha_api_key(api_key)
        ui.print(f"Bocha API key saved: [cyan]{self._mask_key(api_key)}[/cyan]")
        ui.print("[dim]Bocha will be used as fallback between Tavily and DuckDuckGo.[/dim]")
    elif args.strip() == "delete":
        settings.delete_bocha_api_key()
        ui.print("[dim]Bocha API key deleted.[/dim]")
    else:
        ui.print("[dim]Usage: /bocha [set|delete|show][/dim]")
```

### 5. `src/voidx/ui/commands.py`

新增命令列表条目：

```python
("/bocha", "Configure Bocha API key for web search"),
("/bocha delete", "Delete Bocha API key"),
("/bocha set", "Set Bocha API key for web search"),
("/bocha show", "Show Bocha API key status"),
```

### 6. `src/voidx/ui/command_catalog.py`

新增 `/bocha` 的 catalog 映射（仿照 `/tavily`）。

### 7. `src/voidx/ui/gateway/session/method/settings.py`

`_settings_summary()` 中新增 `bocha` 摘要（仿照 `tavily`）。

### 8. `src/voidx/ui/gateway/session/method/integrations.py`

新增 `_bocha_summary()`、`_method_bocha_set()`、`_method_bocha_delete()`（仿照 Tavily）。

### 9. `src/voidx/ui/gateway/session/core.py`

注册 JSON-RPC 方法：

```python
m.register("bocha.set", self._method_bocha_set)
m.register("bocha.delete", self._method_bocha_delete)
```

### 10. `src/voidx/data/documents/voidx-guide/web.md`

更新文档，加入 Bocha 配置说明。

### 11. `src/tests/test_tools/test_web_mcp.py`

新增测试：

- `test_search_bocha_returns_results`：mock Bocha API，验证返回格式
- `test_websearch_falls_through_tavily_to_bocha`：Tavily 失败后降级到 Bocha
- `test_websearch_falls_through_bocha_to_duckduckgo`：Bocha 失败后降级到 DuckDuckGo
- `test_websearch_bocha_skipped_without_key`：无 Bocha Key 时跳过 Bocha
- `test_bocha_api_key_get_set_delete`：配置读写测试
- `test_search_bing_returns_results`：mock Bing HTML，验证 `_BingResultParser` 解析 `b_algo` 块
- `test_websearch_falls_through_duckduckgo_to_bing`：DuckDuckGo 失败后降级到 Bing 爬虫
- `test_websearch_all_backends_fail`：所有后端都失败时返回错误聚合信息

## 不改动的部分

- MCP 路由逻辑不变（优先级最高，`backend="mcp"` 时直接返回）
- DuckDuckGo 实现不变（`_search_duckduckgo()` / `_DDGResultParser` 不改），但 `execute()` 中 DuckDuckGo 块的异常处理从"直接返回错误"改为"记录 fallback_errors 并降级到 Bing"
- `WebSearchInput` schema 不变
- 缓存机制不变（TTL 600 秒）
- `normalize_search_results()` 不变
- Bocha 不自动配置 MCP 路由（和 Tavily 不同，Bocha 没有 MCP server）

## 验证命令

```bash
# 运行 websearch 相关测试
./test.py --backend -- src/tests/test_tools/test_web_mcp.py -v

# 运行所有 web 工具测试
./test.py --backend -- src/tests/test_tools/test_web_mcp.py src/tests/test_tools/test_webfetch.py -v

# 验证配置读写
./test.py --backend -- src/tests/test_config -v
```

## 风险

- **国内无外网场景**：Tavily 和 DuckDuckGo 在国内直连不可达，每次请求会等到超时（15 秒）才降级。如果 Bocha 有 Key，用户只需等 Tavily 超时一次即可命中 Bocha；如果 Bocha 无 Key，需等 Tavily + DDG 两次超时（30 秒）才到 Bing。未来可考虑缩短超时或加网络环境检测优化此场景。
- **Bocha API 稳定性**：Bocha 是国内服务，国际网络环境可能无法访问。降级链设计保证失败时自动降级到 DuckDuckGo 或 Bing。
- **Bocha 响应格式变化**：如果 Bocha 修改 API 响应结构，`_search_bocha()` 可能需要适配。降级链保证即使 Bocha 解析失败也不会阻塞搜索。
- **Bing 反爬风险**：桌面端 UA 会被 captcha 拦截（实测确认），移动端 UA 当前可用但未来可能也被封。Bing 是最终兜底，失败后无更多降级，返回所有后端的错误聚合信息。
- **Bing HTML 结构变化**：Bing 可能修改 `b_algo` class 名或结果块结构。`_BingResultParser` 解析失败时返回空列表，不会阻塞搜索（但会丢失最终兜底能力）。
- **Bing 结果数限制**：移动端 Bing 的 `count` 参数无效，固定返回 5 条结果，无法通过 `max_results` 参数控制。
- **缓存 key 变化**：`backend` 字段格式从 `"tavily"` 变为 `"tavily+bocha+duckduckgo+bing"`，旧缓存会失效（TTL 600 秒后自动过期），不影响功能。
