# Websearch 降级链改进方案

## 目标

在 `websearch` 工具中加入 Bocha 搜索后端，实现 `Tavily → Bocha → DuckDuckGo` 降级链。每个后端"有配置就试，失败就降级"，不配置 Bocha 的用户行为不变。

## 现状

当前 `src/voidx/tools/web/search.py` 的 `execute()` 流程：

1. MCP 路由（如已配置 `backend="mcp"`，直接返回，不走降级链）
2. Tavily（有 Key 才试，失败记录到 `fallback_errors`）
3. DuckDuckGo（兜底，无 Key，HTML 爬取）

缓存 key 的 `backend` 字段只标记 `"tavily"` 或 `"duckduckgo"`，不区分 Bocha。

## 改进方案

### 降级链

```
MCP 路由（最高优先级，全有全无）
  → Tavily（有 Key 才试）
    → Bocha（有 Key 才试）
      → DuckDuckGo（兜底，无 Key）
```

每个后端失败时记录到 `fallback_errors`，降级到下一个。缓存 key 的 `backend` 字段标记所有可能命中的后端（如 `"tavily+bocha+duckduckgo"`），确保不同配置组合不会命中同一缓存。

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

**修改 `execute()` 方法**，在 Tavily 和 DuckDuckGo 之间插入 Bocha：

```python
# 现有 Tavily 块之后：
bocha_key = self._get_bocha_key()
if bocha_key:
    try:
        results = await _search_bocha(
            inp.query,
            bocha_key,
            inp.allowed_domains,
            inp.blocked_domains,
            inp.max_results,
        )
        if results:
            result = self._format_results(inp.query, results, "bocha", fallback_errors)
            WEB_TOOL_CACHE.set(cache_key, result, ttl_seconds=600)
            return result
    except Exception as exc:
        fallback_errors.append(f"bocha: {exc}")

# 现有 DuckDuckGo 块不变
```

**修改缓存 key 计算**，`backend` 字段改为反映所有可能命中的后端：

```python
# 现有：
backend="tavily" if tavily_key else "duckduckgo"
# 改为：
backends = []
if tavily_key:
    backends.append("tavily")
if bocha_key:
    backends.append("bocha")
backends.append("duckduckgo")
backend = "+".join(backends)
```

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

## 不改动的部分

- MCP 路由逻辑不变（优先级最高，`backend="mcp"` 时直接返回）
- DuckDuckGo 兜底不变
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

- **Bocha API 稳定性**：Bocha 是国内服务，国际网络环境可能无法访问。降级链设计保证失败时自动降级到 DuckDuckGo。
- **Bocha 响应格式变化**：如果 Bocha 修改 API 响应结构，`_search_bocha()` 可能需要适配。降级链保证即使 Bocha 解析失败也不会阻塞搜索。
- **缓存 key 变化**：`backend` 字段格式从 `"tavily"` 变为 `"tavily+bocha+duckduckgo"`，旧缓存会失效（TTL 600 秒后自动过期），不影响功能。
