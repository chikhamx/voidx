# Retry Config — goal_resolver / webfetch / mcp transport 重试机制

Date: 2026-07-07

> **Status: Done** — 实现完成，全部测试通过。

## Context

voidx 中有三处网络/LLM 调用缺少重试机制，偶发抖动会直接降级或失败：

1. **`agent/goal_resolver.py:118`** — 顶层 turn 的意图解析调用 `runnable.ainvoke()`，任何异常
   （超时、连接重置、429）直接降级到 `general` intent，导致走错 workflow 路径。用户体验
   层面的损失，非致命。
2. **`tools/webfetch.py:_fetch_url`** — 单次 httpx 请求抓取网页，失败直接返回错误。网页抓取
   偶发超时/连接重置很常见。
3. **`mcp/client/base.py:_request`** — MCP JSON-RPC 请求，`McpConnectionError` /
   `McpTimeoutError` 直接抛出。远程 MCP server（尤其 SSE）偶发不可用是常态。

主对话流 `agent/graph/core/llm.py` 已有独立重试逻辑（`max_retries=5`，线性退避），本次不改。

### 为什么现在做

- 三个调用点各自硬编码或缺失重试，参数分散，无法统一调优。
- 用户反馈偶发 goal resolver 误判走错 workflow，根因是单次调用失败即降级。
- MCP 远程 server 在不稳定网络下频繁报连接错误。

## Goals and Non-Goals

### Goals

- 为三个调用点添加最多 3 次重试，指数退避 + jitter。
- 重试参数统一抽到 `settings.json` 的 `retry` 键下，单一来源。
- 每次重试打印日志（`log_tool_event` 或模块 logger）。
- 向后兼容：不配置 `retry` 键时使用默认值，行为与现在一致（除新增重试外）。

### Non-Goals

- 不改主对话流 `core/llm.py` 的重试逻辑（已有独立机制）。
- 不改 `websearch.py`（已有 Tavily → DuckDuckGo 降级链，非重试场景）。
- 不改 `selfupdate.py`（失败不影响运行）。
- 不改 `llm/catalog.py`（已有静态 fallback）。
- 不引入 `tenacity` 等外部依赖，用内部轻量实现。

## Architecture

### 数据流

```
settings.json ("retry" 键)
    ↓
SettingsRetryMixin.get_retry_config() → RetryConfig
    ↓
三个调用点各自读取 RetryConfig
    ↓
retry_async(coro_fn, config, label, logger)  ← 公共工具函数
    ↓
指数退避 + jitter 重试，每次打日志
```

### 模块边界

| 模块 | 职责 |
|---|---|
| `config/models.py` | `RetryConfig` Pydantic 模型定义 |
| `config/settings_retry.py` | `SettingsRetryMixin`，读写 settings.json 的 `retry` 键 |
| `config/settings.py` | 继承 mixin，注册 `retry` 为 global key |
| `tools/retry.py` | 公共 `retry_async()` 工具函数 |
| `agent/goal_resolver.py` | 包裹 `ainvoke`，重试 + 日志 |
| `tools/webfetch.py` | 包裹 `_fetch_url`，重试 + 日志，排除 SSRF 异常 |
| `mcp/client/base.py` | 包裹 `_request` 内的发送+等待，重试 + 日志，排除协议错误 |

## Data Model

### RetryConfig

```
RetryConfig
├── max_attempts: int (default=3, ge=1, le=10)      # 含首次调用，即最多重试 2 次
├── base_delay: float (default=1.0, ge=0.0, le=60.0) # 首次重试基础延迟（秒）
├── max_delay: float (default=10.0, ge=0.0, le=120.0) # 延迟上限（秒）
└── jitter: bool (default=True)                      # 是否加随机抖动
```

### settings.json 示例

```json
{
  "retry": {
    "max_attempts": 3,
    "base_delay": 1.0,
    "max_delay": 10.0,
    "jitter": true
  }
}
```

### 退避公式

```
delay = min(base_delay * 2^(attempt-1), max_delay)
if jitter:
    delay *= 0.5 + random()   # delay 在 [delay/2, delay] 之间
```

示例（base_delay=1.0, max_delay=10.0, jitter=True）：
- 第 1 次重试：delay = min(1.0, 10.0) = 1.0s → jitter 后 [0.5, 1.0]s
- 第 2 次重试：delay = min(2.0, 10.0) = 2.0s → jitter 后 [1.0, 2.0]s

## API Contract

### retry_async

- **Signature**:
  ```python
  async def retry_async(
      coro_fn: Callable[[], Awaitable[T]],
      *,
      max_attempts: int,
      base_delay: float,
      max_delay: float,
      jitter: bool,
      label: str,
      logger: logging.Logger | None = None,
      retry_on: type[Exception] | tuple[type[Exception], ...] | None = None,
  ) -> T
  ```
- **行为**：
  - 调用 `coro_fn()`，成功则返回结果。
  - 失败时，若 `retry_on` 指定且异常不匹配，直接 raise（不重试）。
  - `retry_on=None` 表示所有异常都重试。
  - 每次重试前 sleep `delay` 秒，并打 warning 日志：`"{label} attempt {n}/{max} failed: {e} — retrying in {delay}s"`。
  - 达到 `max_attempts` 后仍失败，raise 最后一个异常。
- **Errors**: 透传被调用方的异常。

### SettingsRetryMixin

- **`get_retry_config() -> RetryConfig`**：从 `settings.json` 的 `retry` 键读取，无效时返回默认 `RetryConfig()`。
- **`set_retry_config(config: RetryConfig) -> Path`**：写入 `retry` 键，返回保存路径。

### resolve_goal_for_turn（签名变更）

```python
async def resolve_goal_for_turn(
    *,
    model: Any | None,
    user_text: str,
    interaction_mode: str | InteractionMode | None,
    task_state: TaskState,
    log_diagnostic: bool = True,
    retry_config: RetryConfig | None = None,   # 新增
) -> GoalResolution
```

- `retry_config=None` 时用 `RetryConfig()` 默认值。
- 重试包裹在 `asyncio.wait_for` 外层：每次重试重新执行 `asyncio.wait_for(runnable.ainvoke(...), timeout=GOAL_RESOLVER_TIMEOUT_SECONDS)`，即每次调用有独立的 20s timeout。**不要**把重试放在 `wait_for` 内部，否则单个 timeout 会覆盖所有重试。
- 最坏总耗时 = `max_attempts × GOAL_RESOLVER_TIMEOUT_SECONDS`（默认 3 × 20s = 60s）。

### McpClient（构造函数变更）

```python
def __init__(self, config: McpServerConfig, retry_config: RetryConfig | None = None)
```

- `retry_config=None` 时用 `RetryConfig()` 默认值。
- `McpManager` 创建 client 时传入 `settings.get_retry_config()`。

### MCP reconnect 与重试的交互

`_request`（base.py:233-238）在 `not self._healthy` 时会先尝试 `reconnect()`，reconnect 失败抛 `McpConnectionError`。如果重试包裹整个 `_request`，每次重试都会重新进入 reconnect 逻辑，可能形成"重试 → reconnect 失败 → 重试 → reconnect 失败"的放大效应（3 次重试 × 3 次 reconnect = 最多 9 次连接尝试）。

**设计决策**：reconnect 失败的 `McpConnectionError` **不消耗重试次数**。重试只针对"连接已建立但请求失败"的场景（`_send_payload` 抛异常、`McpTimeoutError`）。实现方式：在 `_request` 内部把 reconnect 逻辑包裹在 try/except 中，reconnect 失败时直接 raise 不进入重试循环；只有 `_send_payload` 之后的异常才走 `retry_async`。

具体实现：把 `_request` 拆为两部分——`_ensure_connected()`（含 reconnect，不重试）和 `_send_and_wait()`（发送+等待，可重试）。`retry_async` 只包裹 `_send_and_wait()`。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| goal_resolver 调用超时 | 重试最多 3 次，仍失败则降级到 general intent（保持现有行为） |
| goal_resolver 结构化输出解析失败 | 不重试（`_coerce_resolution` 返回 None 是逻辑错误，非网络问题） |
| webfetch 网络超时/连接重置 | 重试最多 3 次 |
| webfetch SSRF 拦截（PrivateHostBlocked） | 不重试，立即返回 blocked 结果 |
| webfetch HTTP 4xx（404 等） | 不重试，立即返回错误 |
| webfetch HTTP 5xx | 重试 |
| mcp transport 连接失败（reconnect 失败） | 不重试，立即抛出 `McpConnectionError`（避免重试×reconnect 放大） |
| mcp transport 请求发送失败（`_send_payload` 异常） | 重试最多 3 次 |
| mcp transport 超时 | 重试最多 3 次 |
| mcp 协议错误（McpProtocolError） | 不重试，立即抛出 |

### webfetch 异常分类

`retry_async` 的 `retry_on` 参数传入可重试异常元组：

```python
_RETRYABLE_WEBFETCH = (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError)
# HTTPStatusError 会先检查 status_code >= 500
```

`PrivateHostBlocked` 不在元组中，直接抛出由上层捕获返回 blocked 结果。

### mcp 异常分类

```python
_RETRYABLE_MCP = (McpConnectionError, McpTimeoutError)
# McpProtocolError 不重试
```

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 重试参数放 settings.json `retry` 键 | 1) 各调用点硬编码 2) 环境变量 | 用户要求统一配置；settings.json 是 voidx 已有的配置中心，支持 global scope |
| 公共 `retry_async` 函数 | 各调用点内联重试逻辑 | 避免重复，三个点逻辑一致 |
| 指数退避 + jitter | 1) 线性退避 2) 固定延迟 | 指数退避对 429/服务端压力更友好；jitter 避免重试风暴 |
| `max_attempts=3` 含首次调用 | 分开 `max_retries` 和 `max_attempts` | 语义更清晰，`max_attempts=3` = 最多调用 3 次（重试 2 次） |
| goal_resolver 重试后仍失败降级 | 抛错中断 turn | 降级更安全，走 general intent 比中断用户体验好 |
| MCP 重试放在 `_request` 内 | 放在 `call_tool` 外层 | `_request` 级别让 handshake、list_tools 都受益 |
| 不用 tenacity | 用 tenacity | 避免外部依赖，逻辑简单（<30 行） |
| `core/llm.py` 主对话流不改 | 也接入 RetryConfig | 已有独立重试且参数不同（5 次线性退避），改动风险大，本次不碰 |

## Resolved Questions

- **`McpClient.__init__` 加 `retry_config` 参数是否影响现有调用方？**
  已确认两处调用：`mcp/manager.py:252`（传入 settings 读取的 config）和 `slash/mcp.py:383`（默认 None）。默认值 None 保持兼容。
- **goal_resolver 重试时是否需要重新构建 `resolver_messages`？**
  不需要。`resolver_messages` 是无状态的（一个 SystemMessage + 一个 HumanMessage），重试直接复用同一组消息。
- **webfetch 的 4xx/5xx 区分如何实现？**
当前 `_fetch_url` 用 `resp.raise_for_status()`（webfetch.py:250），4xx 和 5xx 都抛 `HTTPStatusError`，调用方（webfetch.py:177）依赖异常走 `except` 分支返回错误。改为：`_fetch_url` 内手动检查 `status_code`，4xx 直接返回 `_FetchResponse`（让上层返回错误 ToolResult），5xx 抛 `httpx.HTTPStatusError` 进入重试。这样 `retry_on` 只需匹配 `httpx.TimeoutException | httpx.NetworkError | httpx.HTTPStatusError`。

**调用方需同步改造**：`_fetch_url` 的调用方（webfetch.py:177-211）当前只在 `except` 里处理错误。4xx 改为返回正常 `_FetchResponse` 后，调用方需在 line 178 之后新增 `if resp.status_code >= 400:` 判断，返回带错误信息的 `ToolResult`（如 `output=f"HTTP {resp.status_code}: ..."`），否则 4xx 响应会被当作正常网页内容提取。

### turn_runner 传参具体写法

`turn_runner.py:203` 当前：
```python
intent_resolution = await resolve_goal_for_turn(
    model=host.model,
    user_text=payload.title_text,
    interaction_mode=interaction_mode,
    task_state=base_task_state,
    log_diagnostic=bool(getattr(host.config, "log_llm_diagnostic", False)),
)
```

改为：
```python
intent_resolution = await resolve_goal_for_turn(
    model=host.model,
    user_text=payload.title_text,
    interaction_mode=interaction_mode,
    task_state=base_task_state,
    log_diagnostic=bool(getattr(host.config, "log_llm_diagnostic", False)),
    retry_config=host._settings.get_retry_config() if host._settings else None,
)
```


## File Inventory

| 文件 | 操作 |
|---|---|
| `src/voidx/config/models.py` | 修改：新增 `RetryConfig` |
| `src/voidx/config/settings_retry.py` | 新建：`SettingsRetryMixin` |
| `src/voidx/config/settings.py` | 修改：继承 mixin，`GLOBAL_KEYS` 加 `"retry"` |
| `src/voidx/config/__init__.py` | 修改：导出 `RetryConfig` |
| `src/voidx/tools/retry.py` | 新建：`retry_async` 工具函数 |
| `src/voidx/agent/goal_resolver.py` | 修改：包裹 `ainvoke`，加 `retry_config` 参数 |
| `src/voidx/agent/graph/turn_runner.py` | 修改：传入 `retry_config` |
| `src/voidx/tools/webfetch.py` | 修改：包裹 `_fetch_url` |
| `src/voidx/mcp/client/base.py` | 修改：`__init__` 加参数，`_request` 加重试 |
| `src/voidx/mcp/manager.py` | 修改：创建 client 时传入 retry_config |
| `src/tests/test_tools/test_retry.py` | 新建：`retry_async` 单元测试 |
| `src/tests/test_llm/test_goal_resolver_retry.py` | 新建：goal_resolver 重试测试 |
| `src/tests/test_tools/test_webfetch.py` | 修改：加重试测试 |
| `src/tests/test_mcp/test_mcp.py` | 修改：加重试测试 |
