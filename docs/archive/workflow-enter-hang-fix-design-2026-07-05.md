# Workflow Enter 卡死修复 — 技术设计文档

> **Status: Done** — 已实现并验证；基于分析文档 `docs/specs/workflow-enter-hang-2026-07-05.md`

Date: 2026-07-05

## Context

桌面端（Web/Desktop 模式）调用 `workflow(action="enter")` 时会永久卡死。根因是
`UiEventBus.request` 无超时等待 consumer 处理，而 `GatewayEventConsumer` mirror 的
`send_text` → `websocket.send` 会因前端 JS 主线程阻塞（markdown + hljs.highlight 同步渲染）
导致 `transport.paused` 永久等待。

分析文档已确认：
- TUI 模式不复现（无 mirror，DockEventConsumer 是纯内存同步）
- 桌面端复现（GatewayEventConsumer mirror → `_broadcast` → `send_text` 阻塞）
- `/clear` hang fix 的 `prefer_direct` 只覆盖 `_show_startup`，未覆盖 `notify_tool_started`

本设计实现两层修复：
1. **兜底防御**（反馈 3）：`UiEventBus.request` 加超时 + 中断传播，超时后终止当前 turn，避免 caller 永久等待
2. **根因缓解**（反馈 2）：gateway broadcast 写路径拆成独立发送 task，`GatewayEventConsumer` 不再等待 `websocket.send`

> 注意：`UiEventBus.request` 超时是 turn 级兜底，不是 event bus 自恢复机制。如果 consumer 已经卡在某个事件处理里，`UiEventBus._run` 仍会等待该 consumer 返回；超时只释放当前 request caller 并终止当前 turn。真正降低卡住概率的是 gateway broadcast 写路径解耦。

## Goals and Non-Goals

### Goals

- `UiEventBus.request` 不再永久阻塞：5s 轮询日志，最多 10 次（50s 上限），超时后终止 turn
- gateway broadcast 写路径解耦：`send_text` 只入发送队列，由独立 asyncio task 执行实际 `websocket.send`
- `GatewayEventConsumer.handle` 不再被 broadcast 的 `websocket.send` 背压阻塞
- 超时后 LLM 调用被终止，turn 正常结束，用户可继续交互
- 前端 markdown 渲染仍由 UI 线程处理（不改 Worker）

### Non-Goals

- 不在本修复中把前端 markdown/render 或 websocket reader 迁移到 Web Worker；本次先做后端兜底和发送背压隔离，前端 Worker 作为后续增强方案
- 不改 `notify_tool_started` 为 `emit`（方向 2，接口改动过大，留后续优化）
- 不保证 `UiEventBus._run` 在 consumer 永久卡死后自动恢复；本次只保证 request caller 有超时兜底
- 不全面重构 gateway request/response 协议；但会避免 `_handle_message` 中新增或保留直接 `websocket.send` 的阻塞路径
- 不覆盖 TUI 模式（不复现）

## Architecture

### 整体数据流（修复后）

```
LLM 生成 workflow enter tool_call
  → execute_tools (executor.py:62)
  → execute_one (executor.py:162)
  → notify_tool_started (ui.py:18)
  → events.request(ToolStarted) (ui.py:31)
  → UiEventBus.request: 超时轮询循环 (bus.py)          ← 反馈3：5s×10次
      ├─ 正常路径: consumer.handle 返回 → future.set_result → request 返回
      └─ 超时路径: 50s 后抛 UiEventTimeout → 中断传播
  → CompositeEventConsumer.handle (consumers.py:69)
    → primary: DockEventConsumer (同步)
    → mirror: GatewayEventConsumer.handle (consumers.py:19)
      → session.broadcast_event (core.py:185)
      → _broadcast (core.py:342)
      → client.send_text → 发送队列                    ← 反馈2：发送 task 分离
        └─ _send_loop task: 从发送队列取消息，执行 websocket.send
```

### 反馈 3：`UiEventBus.request` 超时 + 中断传播

#### 3.1 `request` 超时循环

`bus.py:73-78` 的 `return await future` 改为轮询循环：

```python
# src/voidx/ui/output/events/bus.py

import logging

logger = logging.getLogger(__name__)

class UiEventTimeout(TimeoutError):
    """UiEventBus.request 超时，consumer 未在限定时间内处理事件。"""

async def request(self, event: UiEvent, *, timeout: float = 5.0, max_retries: int = 10) -> Any:
    if not self.is_running or self._queue is None:
        raise RuntimeError("UI event bus is not running")
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    await self._queue.put(_QueuedEvent(event, future))
    for attempt in range(max_retries):
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.TimeoutError:
            if future.done():
                return future.result()
            logger.warning(
                "UiEventBus.request stall: event=%s attempt=%d/%d elapsed=%.1fs",
                type(event).__name__, attempt + 1, max_retries, (attempt + 1) * timeout,
            )
            continue
    future.cancel()
    raise UiEventTimeout(
        f"UiEventBus.request timed out after {max_retries * timeout}s: {type(event).__name__}"
    )
```

关键点：
- `asyncio.shield(future)` 防止 `wait_for` 超时后 cancel 掉 future（consumer 仍可能在处理）
- 每次超时打 warning 日志，含事件类型、尝试次数、累计耗时
- 50s 后 `future.cancel()` + 抛 `UiEventTimeout`
- 当前 `_run` 已在 `set_result` / `set_exception` 前判断 `not item.future.done()`，因此 canceled future 不会触发 `InvalidStateError`
- 该机制只释放等待 `request` 的 caller；如果 consumer 本身永久卡死，event bus 后续事件仍会排队等待，不能把它当成 bus 自恢复

#### 3.2 中断传播路径

`UiEventTimeout` 需要从 `notify_tool_started` 向上传播到 `execute_tools`，最终设置
`should_continue: False` 终止 turn。传播链路：

```
UiEventBus.request 抛 UiEventTimeout
  → notify_tool_started (ui.py:31) 不捕获，向上抛
  → execute_one (executor.py:168) 捕获，标记 turn 终止
  → execute_approved / _execute_approved_batch 传播终止信号
  → execute_tools (executor.py:374) 设置 should_continue=False
  → route_after_execute_tools (topology.py:48) 返回 "end"
  → turn 结束
```

**`execute_one` 的捕获逻辑**（`executor.py:162` 附近）：

```python
from voidx.ui.output.events.bus import UiEventTimeout

async def execute_one(tc):
    tid = tc["name"]
    targs = tc.get("args", {})
    cid = tc.get("id", "")
    tool_event_id = cid or f"{tid}:{id(tc)}"

    try:
        tool_node = await notify_tool_started(host, tc, display_policy)
    except UiEventTimeout:
        return _ExecutedTool(
            message=ToolMessage(
                content=sanitize_tool_message_content(
                    f"Tool notification timed out: UI event bus stalled for {tid}. "
                    "Turn terminated to prevent hang.",
                    workspace=ctx.workspace,
                ),
                tool_call_id=cid,
                status="error",
            ),
            result=ToolResult(
                output="UI event bus timeout",
                metadata={"error": True, "timeout": True},
            ),
            tool_call=tc,
            todo_state=None,
        )
    # ... 原有逻辑
```

**`execute_tools` 的终止判断**（`executor.py:374` 附近，return 之前）：

```python
has_timeout = any(
    getattr(item.result, "metadata", {}).get("timeout")
    for item in executed
    if item.result is not None
)
if has_timeout:
    state_update["should_continue"] = False
    tool_messages.append(AIMessage(content=(
        "Turn terminated: UI event bus timed out while notifying tool start. "
        "This usually indicates the frontend is unresponsive. "
        "The session is still alive — you can continue interacting."
    )))
```

### 反馈 2：Gateway broadcast 发送 task 分离

#### 2.1 问题

当前 `server.py:58-68` 的 `_handle` 里，读循环和写操作共享同一个 event loop；
`_broadcast`（`core.py:342`）的 `client.send_text` → `websocket.send` 如果阻塞
（`transport.paused`），会阻塞 `CompositeEventConsumer.handle` 的 `asyncio.gather`，
进而阻塞 `UiEventBus._run`。

```python
async def _handle(self, websocket: ServerConnection) -> None:
    client = _WebSocketClient(websocket)
    await self._session.connect(client)
    try:
        async for message in websocket:
            await self._handle_message(websocket, str(message))
    finally:
        self._session.disconnect(client)
```

#### 2.2 方案：`_WebSocketClient` 内部发送队列 + bounded/coalescing

将 `_WebSocketClient` 改为发送队列模型：发送操作只入队，由独立 asyncio task 负责实际
`websocket.send`；broadcast caller 不再等待 socket 写完成。

队列需要 bounded，避免前端长期不读时内存无限增长。丢弃策略按消息类型分层：

- `snapshot` / refresh 类事件：只保留最新一条，旧 snapshot 可被覆盖（coalescing）
- streaming/text 增量事件：默认尽量保留，但队列满时记录 warning 并丢弃最旧低优先级事件
- request/response 错误响应：优先级高，不应被普通 broadcast 事件挤掉

```python
# src/voidx/ui/gateway/server.py

import asyncio
import contextlib

SEND_QUEUE_MAXSIZE = 256

class _WebSocketClient:
    """带发送队列的 WebSocket 客户端封装。

    send_text 只入队，由 _send_loop task 负责实际发送。
    这样 websocket.send 阻塞时不会阻塞 GatewayEventConsumer / UiEventBus。
    """

    def __init__(self, websocket: ServerConnection) -> None:
        self._websocket = websocket
        self._send_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=SEND_QUEUE_MAXSIZE)
        self._send_task: asyncio.Task[None] | None = None
        self._closed = False
        self._dropped_messages = 0

    async def start(self) -> None:
        self._send_task = asyncio.create_task(self._send_loop(), name="ws-send-loop")

    async def send_text(self, text: str, *, priority: bool = False) -> None:
        if self._closed:
            return
        try:
            self._send_queue.put_nowait(text)
            return
        except asyncio.QueueFull:
            if priority:
                await self._drop_one_low_priority_message()
                self._send_queue.put_nowait(text)
                return
            self._dropped_messages += 1
            # logger.warning("Gateway send queue full; dropping message count=%d", self._dropped_messages)

    async def _drop_one_low_priority_message(self) -> None:
        # 初版可简单丢弃队首；后续可解析 envelope，优先丢 snapshot/refresh。
        with contextlib.suppress(asyncio.QueueEmpty):
            self._send_queue.get_nowait()
            self._send_queue.task_done()

    async def _send_loop(self) -> None:
        try:
            while True:
                text = await self._send_queue.get()
                if text is None:
                    return
                try:
                    await self._websocket.send(text)
                except Exception:
                    self._closed = True
                    return
                finally:
                    self._send_queue.task_done()
        finally:
            self._closed = True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._send_task is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._send_queue.put_nowait(None)
            try:
                await asyncio.wait_for(self._send_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._send_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._send_task
        with contextlib.suppress(Exception):
            await self._websocket.close()
```

**`_handle` 改造**：

```python
async def _handle(self, websocket: ServerConnection) -> None:
    if not self._authorized(websocket):
        await websocket.close(code=1008, reason="unauthorized")
        return
    client = _WebSocketClient(websocket)
    await client.start()
    await self._session.connect(client)
    try:
        async for message in websocket:
            await self._handle_message(client, str(message))
    finally:
        self._session.disconnect(client)
        await client.close()
```

`finally` 中先 `disconnect` 再 `close`，减少 close 等待期间仍被 session broadcast 入队的窗口。

#### 2.3 `_handle_message` 直接发送路径也必须走队列

当前 `server.py` 里 `_handle_message` 的错误响应和 request result 直接调用 `websocket.send`。
这些路径虽然不是 `workflow(action="enter")` 的主触发点，但仍可能在背压下阻塞 `_handle`
读循环。因此改造后 `_handle_message` 接收 `_WebSocketClient`，所有响应统一走
`client.send_text(...)`。

为避免未来新增直接 `websocket.send`，同时抽一个统一发送 helper：

```python
async def _send_json(client: _WebSocketClient, payload: dict[str, object]) -> None:
    await client.send_text(json.dumps(payload), priority=True)
```

```python
async def _handle_message(self, client: _WebSocketClient, raw: str) -> None:
    try:
        msg = parse_jsonrpc_message_str(raw)
    except ParseError as exc:
        await self._send_json(client, {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": exc.code, "message": exc.message},
        })
        return

    if isinstance(msg, JsonRpcRequest):
        result = await self._session.dispatch_request(msg)
        await client.send_text(result.model_dump_json(), priority=True)
    elif isinstance(msg, JsonRpcResult):
        # 原有响应处理逻辑不发送数据，保持不变
        ...
```

#### 2.4 前端读取、UI CPU 渲染与 websocket 背压

当前前端 websocket 接收入口在 `frontend/src/rpc.ts`：`_setSocket` 注册 `ws.addEventListener("message", handleMessage)`，`handleMessage` 在浏览器 JS 主线程解析 JSON 并同步分发 notification handler。`frontend/src/main.ts` 的 `handleNotification` 会继续触发 transcript/render/markdown/highlight 等 UI 更新。

前端 markdown / highlight 渲染是 CPU 密集型，发生在浏览器 UI 主线程。后端 Python asyncio 不和浏览器共享同一个协程上下文，也不共享同一个线程；但两者通过同一个 websocket TCP 连接耦合。当前端 UI 主线程长时间执行同步渲染时：

- 浏览器网络栈仍可能在底层接收部分数据，但 JS `message` callback 不能及时运行
- `handleMessage` 不能及时 parse / dispatch，前端应用层没有持续 drain websocket 消息
- 前端也不能及时发送 JSON-RPC response / ACK 类应用响应
- 浏览器/socket buffer 可能逐步堆积，背压传导到后端 websocket transport
- 后端 `await websocket.send(...)` 可能等待 `transport.paused` 恢复，长时间不返回

因此问题不是“前端 CPU 直接阻塞后端 event loop”，而是：前端 CPU 密集渲染让浏览器不能及时消费 websocket 数据，连接背压再传导到后端 `websocket.send`。

“单独协程”需要区分两种情况：

- **同一个 JS 主线程里的 Promise / async / setTimeout / requestIdleCallback**：不能真正隔离 CPU 密集渲染；只要主线程被 markdown/highlight 占满，message handler 仍然不会运行
- **Web Worker 独立线程**：可以让 Worker 持有 WebSocket、读取消息、JSON.parse、做队列/coalescing，再通过 `postMessage` 批量交给 UI 主线程渲染；这才是真正意义上的前端读取隔离

本次方案不引入前端 Web Worker，原因是用户已否决把 markdown 渲染迁移 Worker，且 Worker 化 websocket reader 会扩大协议和状态同步改动。当前后端修复的目标是：即使前端主线程因为渲染不能及时读取 websocket，也把背压限制在后端 `_send_loop` task 和 bounded queue 内，不再拖死 `GatewayEventConsumer.handle` / `UiEventBus._run` / 当前 turn。

如果后续要更彻底解决前端读取问题，可单独设计：

- Worker 持有 WebSocket，主线程不直接操作 socket
- Worker 负责 JSON parse、request/response correlation、snapshot coalescing、低优先级消息丢弃
- 主线程通过 Worker 发送 rpcCall/rpcNotify/rpcRespond
- UI 渲染继续在主线程，但要配合批量渲染、虚拟列表、分片 markdown/highlight，避免 `postMessage` 到达后再次长时间卡住主线程

#### 2.5 为什么这样能缓解根因

- `send_text` 变成快速入队，正常情况下不等待实际 socket 写完成
- `websocket.send` 的阻塞只影响 `_send_loop` task，不影响 `GatewayEventConsumer.handle`
- consumer 快速返回后，`UiEventBus._run` 不再因为 gateway broadcast 写阻塞而卡住
- bounded queue 避免前端长期不读时内存无限增长
- `_handle_message` 的响应路径也走队列，避免保留新的直接写阻塞点
- 前端 JS 主线程阻塞时，`_send_loop` 可能等待 `websocket.send`，但不会阻塞 event bus consumer

#### 2.6 `_broadcast` 的错误处理调整

`core.py:342-349` 的 `_broadcast` 可以保持 try/except 结构不变。`send_text` 现在主要负责入队，
只有队列/客户端状态异常才会抛；保留 `return_exceptions=True` 兼容 FakeClient 测试和未来变更。

```python
async def _broadcast(self, text: str) -> None:
    clients = tuple(self._clients)
    results = await asyncio.gather(
        *(client.send_text(text) for client in clients),
        return_exceptions=True,
    )
    for client, result in zip(clients, results, strict=False):
        if isinstance(result, Exception):
            self._clients.discard(client)
```

## API Contract

### `UiEventBus.request`

- **Signature**: `async def request(self, event: UiEvent, *, timeout: float = 5.0, max_retries: int = 10) -> Any`
- **正常返回**: consumer.handle 的返回值
- **超时**: 抛 `UiEventTimeout`（继承 `TimeoutError`）
- **日志**: 每次超时打 `WARNING` 级别日志
- **限制**: 超时只释放 caller，不保证恢复已卡住的 consumer / `_run` task

### `_WebSocketClient`

- **队列上限**: 使用 `SEND_QUEUE_MAXSIZE = 256` 作为初始 bounded queue 上限，避免前端长期不读时内存无限增长
- **`async def start() -> None`**: 启动 `_send_loop` task
- **`async def send_text(text: str, *, priority: bool = False) -> None`**: 入队，不等待 websocket 写完成；队列满时普通消息可丢弃/合并，priority 消息先丢弃低优先级旧消息后入队。`_drop_one_queued_message` 按 envelope `method` 字段优先丢弃 `workspace.snapshot` / `refresh.requested` 类消息，无 droppable 消息时 fallback 到队首
- **`ProtocolClient` 接口**: `send_text` 签名与 `_WebSocketClient` 一致，含 `*, priority: bool = False` 参数
- **`async def close() -> None`**: 发送 None 信号，等待 `_send_loop` 结束；5s 超时后 cancel 并关闭 websocket
- **统一发送 helper**: `_handle_message` 的 JSON 响应统一走 `_send_json(client, payload)` / `client.send_text(..., priority=True)`，禁止新增直接 `websocket.send`

### `execute_one` 超时处理

- **捕获**: `UiEventTimeout`
- **返回**: `_ExecutedTool` 含 `metadata={"timeout": True, "error": True}` 的 ToolResult
- **不抛异常**: 避免中断 `_execute_approved_batch` 的批处理
- **同批次行为**: 超时后同批次其他工具仍可能继续执行；`execute_tools` 最终检测到 `metadata.timeout` 后统一设置 `should_continue=False` 终止 turn

### `execute_tools` 终止判断

- **检测**: `any(item.result.metadata.get("timeout") for item in executed)`
- **动作**: `state_update["should_continue"] = False` + 追加 AIMessage 说明

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| `request` 超时（50s） | 抛 `UiEventTimeout`，`execute_one` 捕获，当前 turn 终止 |
| `request` 超时后 consumer 仍在处理 | `future.cancel()`；`_run` 的 done-check 避免 set_result/set_exception 报错，但 event bus 不保证自恢复 |
| `_send_loop` 内 `websocket.send` 异常 | 标记 `_closed`，`_send_loop` 退出，后续 `send_text` no-op |
| 发送队列满 | 普通 broadcast 消息丢弃/合并并记录 warning；priority request/response 先腾挪低优先级消息再入队 |
| `client.close()` 时 `_send_loop` 不退出 | 5s 超时后 cancel task，suppress `CancelledError`，再关闭 websocket |
| `_handle_message` 需要发送错误/result | 统一走 `_send_json` / `client.send_text(..., priority=True)`，避免直接 `websocket.send` 阻塞读循环 |
| 超时后 turn 终止但 session 仍存活 | AIMessage 提示用户可继续交互，不关闭 session |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| `request` 用轮询而非单次 `wait_for` | 单次 `wait_for(future, 50)` | 轮询可每 5s 打日志，便于定位 stall；单次超时无中间可观测性 |
| `asyncio.shield(future)` | 不 shield | 不 shield 的话 `wait_for` 超时会 cancel future，consumer 的结果无法被正常保留到最终超时判断前 |
| 超时后 `future.cancel()` | 不 cancel | 明确 caller 已放弃等待；当前 `_run` 有 done-check，不会对 canceled future set_result |
| `_WebSocketClient` 用 asyncio.Queue + task | 用 `threading.Thread` + `queue.Queue` | 项目全栈 asyncio，混用线程需 `run_coroutine_threadsafe`，复杂度更高 |
| 发送队列使用 bounded queue | 无界队列 | 前端长期不读时无界队列会涨内存；bounded queue 让退化行为可控、可观测 |
| snapshot/refresh 可 coalescing | 所有消息严格保序保留 | snapshot 是状态快照，旧快照价值低；优先保留最新状态能降低堆积 |
| `_handle_message` 响应也走 client 队列 | 只改 broadcast | 保留直接 `websocket.send` 会留下读循环阻塞点，和读写解耦目标冲突 |
| `execute_one` 捕获 `UiEventTimeout` 而非向上抛 | 向上抛到 `execute_tools` | 抛到 `execute_tools` 会中断 `_execute_approved_batch` 批处理；捕获后返回 error ToolResult 更可控 |
| 超时后终止 turn 而非仅返回 None | `request` 返回 None，`notify_tool_started` 走 fallback | 返回 None 后 `execute_one` 会继续执行工具，turn 不会终止，用户仍卡住 |
| 发送 task 分离而非前端 Worker | markdown 渲染迁移 Worker | 用户已否决 Worker；后端需要先避免 broadcast 写阻塞 consumer |

## Implementation Status

### 已完成

- `src/voidx/ui/output/events/bus.py`: 新增 `UiEventTimeout`，`UiEventBus.request` 支持 `timeout` / `max_retries`，使用 `asyncio.shield` 做轮询超时，避免 caller 永久等待。
- `src/voidx/ui/output/events/bus.py`: `UiEventBus.request` stall / timeout 已写入 `tool_log`，事件名为 `ui_event_bus_request_stall` / `ui_event_bus_request_timeout`。
- `src/voidx/agent/graph/tool_executor/executor.py`: `execute_one` 捕获 `UiEventTimeout` 并返回 error `ToolMessage`，`execute_tools` 检测 timeout metadata 后设置 `should_continue=False` 终止当前 turn。
- `src/voidx/ui/gateway/server.py`: `_WebSocketClient` 改为 bounded 发送队列 + 独立 `_send_loop`，`send_text` 只入队，priority 响应可在队列满时腾挪低优先级消息。
- `src/voidx/ui/gateway/server.py`: `_send_loop` 增加单条 `websocket.send` 超时兜底（默认 30s），send timeout / send exception / queue full drop 均写 warning/exception 日志并写入 `tool_log`。
- `src/voidx/ui/gateway/server.py`: `_handle_message` 的 ParseError / JsonRpcRequest 响应统一走 `_send_json` / `client.send_text(..., priority=True)`，不再直接 `websocket.send`。
- `src/voidx/ui/output/events/__init__.py`: 导出 `UiEventTimeout` 作为事件 API 的公共异常类型。
- `frontend/src/rpc-worker.ts`: 新增 Worker reader，Worker 持有 WebSocket、接收 message、转发 open/close/error/message 给主线程。
- `frontend/src/rpc.ts`: 新增 `createWorkerSocket` transport，主线程 RPC 通过 Worker send/receive，同时保留 `_setSocket` 的测试/降级路径。
- `frontend/src/main.ts`: gateway 连接入口改为使用 `createWorkerSocket(url)`，WebSocket 读取从 UI 主线程迁移到 Worker。

### 闭环决策

- `request` 超时后 consumer 仍在处理旧事件：保持队列语义，不主动跳过旧事件；该超时是 turn 级兜底，不是 event bus 自恢复。
- `_send_loop` 单条 `websocket.send` 超时：已实现单条 send timeout，默认 `WEBSOCKET_SEND_TIMEOUT_SECONDS = 30.0`；超时后记录 warning、写入 `tool_log`、关闭 client，阻塞仍限制在 `_send_loop`。
- 发送队列 maxsize / 丢弃策略：已实现 bounded queue，普通 broadcast 可丢弃计数并记录 warning / `tool_log`，priority request/response 可腾挪队列后入队。`_drop_one_queued_message` 按 envelope `method` 字段优先丢弃 `workspace.snapshot` / `refresh.requested` 类消息，无 droppable 消息时 fallback 到队首。
- 前端 websocket reader 迁移 Web Worker：已实现；Worker 持有 WebSocket 并负责读取消息，主线程只接收 Worker 转发并执行 UI 渲染。
- 超时日志上报 tool_log：已实现；`UiEventBus.request` stall/timeout、gateway send queue full、gateway websocket send timeout、gateway websocket send failed 均写入 `tool_log`。

### Verification

已执行并通过：

```bash
cd frontend && npm test
```

结果：`14 passed, 255 tests passed`。测试 stderr 中仍有现有 jsdom `alert()` 未实现和 startup settings fallback 日志，不是本次改动失败。

```bash
cd frontend && npm run build
```

结果：构建通过，产物包含 `dist/assets/rpc-worker-*.js`。

```bash
./python.sh -m pytest tests/test_ui/gateway/ tests/test_agent/graph/test_execute_tools_guard.py -q
```

结果：`218 passed, 11 warnings`。warnings 来自现有 `tests/test_ui/gateway/test_terminal.py` fork deprecation，不是本次改动失败。

新增/覆盖测试：

- `frontend/test/rpc.test.ts`: Worker-owned WebSocket transport，Worker open/message/send 路由保持 RPC 行为正确。
- `tests/test_ui/gateway/test_ui_events_dock_bus.py`: request 超时、consumer 后续完成不触发 canceled future 异常、快速 consumer 正常返回、request timeout 写入 `tool_log`。
- `tests/test_ui/gateway/test_gateway_v2_server.py`: blocked websocket send 不阻塞 `send_text`，priority 消息可在满队列下入队，JSON-RPC 响应走 queued send，单条 send timeout 会关闭 client 并记录 warning / `tool_log`，队列满会记录 drop warning / `tool_log`。
- `tests/test_agent/graph/test_execute_tools_guard.py`: tool start notification timeout 会生成 error ToolMessage，并设置 `should_continue=False`。

### Follow-ups

- 前端渲染优化：Web Worker 已隔离 websocket 读取；后续仍可用虚拟列表、批量 DOM 更新、分片 markdown/highlight 继续减少 UI 主线程长时间 CPU 占用。
- 可观测性增强：当前已覆盖 logger warning/exception 与 `tool_log`；后续可继续接入 metrics / UI diagnostics。
