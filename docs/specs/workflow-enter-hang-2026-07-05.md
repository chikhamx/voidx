# Workflow Enter 卡死问题分析

> **Status: Analysis** — 问题分析阶段，尚未实现修复

Date: 2026-07-05

## Context

用户报告 `workflow(action="enter")` 工具调用会"直接卡在工具调用里没出来，卡死"。
表现为 UI 显示工具正在执行，但永远不返回，turn 无法结束。

这和 2026-06-06 修复的 `/clear` hang（见 `docs/archive/2026-06-07/clear-hang-fix-design-2026-06-06.md`）
是**同一类根因**——`UiEventBus.request()` 无超时等待 consumer 处理事件——但 `/clear` 的修复
只覆盖了 `_show_startup`，没有覆盖 `notify_tool_started`。

## Goals and Non-Goals

### Goals

- 精确定位 `workflow enter` 卡死的完整调用链和阻塞点
- 分析所有 UI 模式（TUI / Web / Desktop）下的触发条件
- 提出可落地的修复方向，供后续实现参考

### Non-Goals

- 本文不包含修复实现代码
- 不覆盖 LLM 循环调用 enter 的 guard 问题（已有 `c5e172d3` 部分修复）

## 根因

### 直接原因：`UiEventBus.request` 无超时

`notify_tool_started` 通过 `events.request(ToolStarted)` 发送工具开始事件，并**等待 consumer
处理完毕**才返回。`request` 内部 `await future` **没有超时**：

```python
# src/voidx/ui/output/events/bus.py:73-78
async def request(self, event: UiEvent) -> Any:
    if not self.is_running or self._queue is None:
        raise RuntimeError("UI event bus is not running")
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    await self._queue.put(_QueuedEvent(event, future))
    return await future  # ← 无超时，永久等待
```

future 的结果由 `UiEventBus._run` Task 设置：

```python
# src/voidx/ui/output/events/bus.py:96-118 (简化)
async def _run(self) -> None:
    while True:
        item = await self._queue.get()
        try:
            result = self._consumer.handle(item.event)       # ← 串行处理
            if inspect.isawaitable(result):
                result = await result
        except BaseException as exc:
            if item.future is not None and not item.future.done():
                item.future.set_exception(exc)
        else:
            if item.future is not None and not item.future.done():
                item.future.set_result(result)                # ← 只有这里能解除 request 的等待
        finally:
            self._queue.task_done()
```

**如果 `consumer.handle(event)` stall，future 永远不被 set_result，`request` 永久阻塞。**

### 卡死链路

```
LLM 生成 workflow enter tool_call
  → _router 返回 "execute"
  → execute_tools
  → notify_tool_started (executor.py:166)
  → events.request(ToolStarted) (ui.py:31)
  → UiEventBus.request: await future (bus.py:78)
  → UiEventBus._run: consumer.handle(ToolStarted) (bus.py:107)
  → CompositeEventConsumer.handle (consumers.py:69)
    → primary: DockEventConsumer.handle (同步，返回 node)
    → mirror: GatewayEventConsumer.handle (consumers.py:19)
      → session.broadcast_event (core.py:160)
      → _broadcast (core.py:306)
      → client.send_text (server.py:17)
      → websocket.send (websockets 库)
      → send_context → await self.drain() (websockets 内部)
      → if self.paused: await waiter  ← 永久阻塞
  → future.set_result 永远不执行
  → request 永久等待
  → notify_tool_started 永久不返回
  → execute_tools 永远不结束
  → turn 卡死
```

### consumer stall 的触发条件

#### Web / Desktop 模式（`CompositeEventConsumer` + `GatewayEventConsumer` mirror）

`CompositeEventConsumer.handle` 先执行 primary（DockEventConsumer，同步），然后 `await
asyncio.gather(*mirror_tasks)` 等待所有 mirror 完成：

```python
# src/voidx/ui/output/events/consumers.py:69-80
async def handle(self, event: UiEvent) -> Any:
    result = self._primary.handle(event)
    if inspect.isawaitable(result):
        result = await result
    mirror_tasks = []
    for mirror in self._mirrors:
        mirror_result = mirror.handle(event)
        if inspect.isawaitable(mirror_result):
            mirror_tasks.append(mirror_result)
    if mirror_tasks:
        await asyncio.gather(*mirror_tasks)  # ← mirror stall 会阻塞这里
    return result
```

`GatewayEventConsumer.handle` → `broadcast_event` → `_broadcast` → `client.send_text` →
`websocket.send`。`websockets` 库的 `send` 在写入数据后调用 `send_context` → `await
self.drain()`：

```python
# websockets.asyncio.server.ServerConnection.drain() (简化)
async def drain(self) -> None:
    if self.transport.is_closing():
        await asyncio.sleep(0)
    if self.paused:                          # ← 发送缓冲区超过高水位线
        waiter = self.loop.create_future()
        self.drain_waiters.append(waiter)
        await waiter                         # ← 永久等待，直到对端读取数据降低水位
```

**本地连接 transport.paused 的原因**：前端 JS 主线程被同步操作阻塞（如 `renderMarkdown`
的大段 markdown + `hljs.highlight` 代码高亮），导致浏览器无法及时读取 WebSocket 接收缓冲区，
TCP 流控暂停服务端发送。

前端 markdown 渲染是同步的：

```typescript
// frontend/src/markdown.ts:30-57
export function renderMarkdown(text: string): HTMLElement {
  const html = marked.parse(text || "", { async: false }) as string;  // 同步
  container.innerHTML = DOMPurify.sanitize(html);                     // 同步
  container.querySelectorAll("pre code").forEach((block) => {
    block.innerHTML = hljs.highlight(block.textContent ?? "", {       // 同步，大段代码很慢
      language: lang,
    }).value;
  });
}
```

当工具输出包含大段代码时，`hljs.highlight` 可能阻塞 JS 主线程数百毫秒甚至数秒，
期间浏览器无法处理 WebSocket 消息。

#### TUI 模式（`DockEventConsumer` 单独）

TUI 模式下没有 GatewayEventConsumer mirror，`DockEventConsumer.handle` 是同步纯内存操作，
**不会永久 stall**。但 `dock.refresh()` 会同步调用 `_live.update(self._render(), refresh=True)`，
如果 dock tree 内容极多，`_render()` 耗时长，会阻塞 `UiEventBus._run` Task，导致队列积压。
这通常只是慢，不是永久卡死。

### 为什么 `/clear` hang fix 没有覆盖这个场景

`/clear` 的修复（`clear-hang-fix-design-2026-06-06.md`）只给 `_show_startup` 加了
`prefer_direct` 参数，在 `/clear` 场景下绕过 `events.request`，直接调 dock：

```python
# src/voidx/agent/graph/run_loop.py:59-64
startup_via_event = active_dock is not None and self._ui.events.is_running and not prefer_direct
if startup_via_event:
    await self._ui.events.request(startup_event)
    ...
    return
# else: 直接调 dock.append_startup，不走 request
```

但 `notify_tool_started`（`ui.py:29-39`）**没有 `prefer_direct` 机制**，始终走
`events.request`。所有工具的 ToolStarted 事件都受此影响，workflow enter 只是其中
最容易被触发的（因为它是 barrier 工具，且 LLM 经常调用）。

### 为什么 workflow enter 特别容易触发

1. **workflow 是 barrier 工具**（`helpers.py:186`）：`_is_barrier_tool` 返回 True，
   workflow 被单独拆出串行执行，ToolStarted 事件必须等待前序事件处理完
2. **workflow display_mode = HIDDEN**（`display_policy.py:116`）：虽然 HIDDEN 模式下
   DockEventConsumer 返回 None，但 `events.request` 仍然被调用，仍然等待 consumer 处理
3. **LLM 频繁调用 enter**：workflow enter 是最常见的 workflow 操作，触发概率高

## 影响范围

| 维度 | 影响 |
|------|------|
| 受影响工具 | **所有工具**的 `notify_tool_started`（不只 workflow） |
| Web/Desktop 模式 | consumer stall 可导致永久卡死 |
| TUI 模式 | 通常只是慢（同步渲染阻塞），不会永久卡死 |
| 已有缓解 | `/clear` 的 `prefer_direct` 只覆盖 `_show_startup` |
| Guard 无效 | runtime_guards 无法检测此卡死（不是 LLM 循环，是工具执行内部阻塞） |

## 关键证据

| 文件 | 行 | 问题 |
|------|------|------|
| `src/voidx/ui/output/events/bus.py` | 73-78 | `request` 无超时 `await future` |
| `src/voidx/ui/output/events/bus.py` | 107-109 | `_run` 串行 `await consumer.handle`，一个 stall 阻塞全部 |
| `src/voidx/ui/output/events/consumers.py` | 78-79 | `CompositeEventConsumer` 的 `asyncio.gather` 等 mirror 完成 |
| `src/voidx/ui/gateway/session/consumer.py` | 19-20 | `GatewayEventConsumer.handle` → `broadcast_event` |
| `src/voidx/ui/gateway/session/core.py` | 306-313 | `_broadcast` 的 `send_text` 无超时 |
| `src/voidx/ui/gateway/server.py` | 17-18 | `send_text` → `websocket.send` 无超时 |
| `src/voidx/agent/graph/tool_executor/ui.py` | 31 | `notify_tool_started` 用 `events.request`（无 `prefer_direct`） |
| `src/voidx/agent/graph/tool_executor/helpers.py` | 186 | workflow 是 barrier 工具，串行执行 |
| `src/voidx/ui/output/display_policy.py` | 116 | workflow display_mode = HIDDEN，但仍走 `request` |
| `docs/archive/2026-06-07/clear-hang-fix-design-2026-06-06.md` | 60-62 | 已知 `request` stall 问题，但只修了 `_show_startup` |

## 修复方向

以下方向供后续实现参考，需要单独的 plan 文档细化。

### 方向 1：`UiEventBus.request` 加超时（最小改动）

```python
async def request(self, event: UiEvent, *, timeout: float = 5.0) -> Any:
    ...
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        return None
```

- **优点**：一处改动覆盖所有 `request` 调用点
- **缺点**：超时后 consumer 仍在处理旧事件，可能产生乱序；tool_node 为 None 时后续
  `notify_tool_result` 走 fallback 路径

### 方向 2：`notify_tool_started` 用 `emit` 替代 `request`

ToolStarted 事件的返回值（tool_node）在 HIDDEN 模式下是 None，在非 HIDDEN 模式下
用于 dock 路径。可以改为 `emit`（不等待），然后在需要 tool_node 时从 dock 直接获取：

```python
# 改为 emit，不等待 consumer
host._ui.events.emit(ToolStarted(...))
tool_node = host._ui.dock.get_tool_node(tool_event_id)  # 从 dock 直接取
```

- **优点**：彻底消除 `request` 卡死风险；emit 是 fire-and-forget
- **缺点**：需要 dock 支持 `get_tool_node` 查询；非 HIDDEN 工具的 tool_node 获取时序变化

### 方向 3：`CompositeEventConsumer` mirror 不阻塞 primary

mirror 的 stall 不应阻塞 primary 的 future：

```python
async def handle(self, event: UiEvent) -> Any:
    result = self._primary.handle(event)
    if inspect.isawaitable(result):
        result = await result
    # mirror 用 create_task 异步执行，不等待
    for mirror in self._mirrors:
        mirror_result = mirror.handle(event)
        if inspect.isawaitable(mirror_result):
            asyncio.create_task(mirror_result)  # ← 不 await
    return result
```

- **优点**：mirror 慢/卡不影响 primary 返回
- **缺点**：mirror 错误丢失（无 await 无法捕获异常）；需要加错误日志和清理

### 方向 4：`WebSocketClient.send_text` 加发送超时

```python
async def send_text(self, text: str) -> None:
    await asyncio.wait_for(self._websocket.send(text), timeout=10.0)
```

- **优点**：直接在 WebSocket 层兜底，防止 send 永久阻塞
- **缺点**：超时后连接状态可能不一致；需要处理 `TimeoutError` 后的连接清理

### 推荐组合

方向 1（request 超时）+ 方向 3（mirror 不阻塞）+ 方向 4（send 超时）三层防御。
方向 2 作为后续优化，需要更多接口改动。

## Open Questions

- [ ] TUI 模式下是否也出现过此卡死？还是仅 Web/Desktop？
- [ ] 前端 `renderMarkdown` 的同步阻塞是否需要改为 Web Worker 或分片渲染？
- [ ] `events.request` 的超时值设多少合适？（5s? 10s?）
- [ ] 超时后是否需要标记 consumer 为 unhealthy 并重启事件总线？
