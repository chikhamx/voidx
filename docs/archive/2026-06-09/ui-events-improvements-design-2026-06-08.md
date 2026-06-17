# UI 事件系统完善度评估与改进设计

> **Status: Done**

## 背景

UI 事件系统是 voidx 终端渲染的核心管道，负责将 agent 输出（流式文本、工具调用、状态更新等）通过异步事件总线传递到 TUI 渲染层。该系统已经相当成熟，但经过审计发现若干可改进之处。

## 当前架构概览

### 三层结构

```
Schema (schema.py)          Bus (__init__.py)           Consumer (__init__.py)
┌─────────────────┐    ┌──────────────────┐    ┌──────────────────────┐
│ 32 个事件类型    │───▶│ UiEventBus       │───▶│ DockEventConsumer    │
│ Literal["kind"] │    │ emit()           │    │ match/case 分发      │
│ Pydantic frozen │    │ emit_nowait()    │    │ → BottomInputDock    │
│                 │    │ emit_direct()    │    │ → _tool_nodes 追踪   │
│                 │    │ request()        │    │ → _agent_nodes 追踪  │
│                 │    │ CompositeConsumer│    │ → todo 渲染          │
└─────────────────┘    └──────────────────┘    └──────────────────────┘
```

### 事件分类

| 类别 | 事件 | 数量 |
|------|------|------|
| 流式 | `AssistantStreamStarted/Updated/Committed/Discarded` | 4 |
| 工具 | `ToolStarted/Finished`, `ToolResultAppended` | 3 |
| 状态 | `StatusUpdated/Finished`, `TurnStarted` | 3 |
| 内容 | `MessageAppended`, `MarkdownAppended`, `ThoughtAppended`, `DiffAppended`, `AnsiAppended` | 5 |
| 交互 | `PermissionPromptShown/Cleared`, `GuidanceSubmitted` | 3 |
| 子代理 | `SubagentStarted/Finished/StepStarted` | 3 |
| 其他 | `CaptureStarted/Stopped`, `RefreshRequested`, `ResetRequested`, `StartupShown`, `TodoUpdated`, `ErrorAppended`, `WarningAppended`, `FileChangeAppended` | 9 |
| 内部 | `InputSet`, `NoticeSet` | 2 |
| **合计** | | **32** |

说明：`PermissionToolDetail` 是 `PermissionPromptShown.tools` 的 payload model，不是独立事件类型。

## 审计发现

### 1. Todo 渲染常量命名混淆

**问题**：`TODO_MAX_VISIBLE_ITEMS`、`TODO_STATUS_ORDER`、`TODO_ICONS` 中的 `TODO` 容易被误认为是代码中的待办标记（`grep TODO` 会命中），实际是 todo 功能的常量。

**建议**：本期先添加模块级注释，明确这些常量是 todo feature 的 render constants，不是代码待办标记。暂不改成 `TASK_*`，避免和 agent task state 概念混淆。

**影响范围**：`events/__init__.py` 内部使用，无外部 API 影响。

### 2. `DockEventConsumer` fail-fast 行为缺少测试覆盖

**现状**：`handle()` 方法使用 `match/case` 分发事件，末尾已经有 `case _:`，并通过 `TypeError(f"Unsupported UI event: {event!r}")` fail-fast。新增 schema 事件但忘记在 consumer 中添加 case 时，不会静默忽略。

**问题**：现有 fail-fast 行为缺少显式测试。未来如果有人删除 `case _:` 或改成静默返回，测试不会及时暴露。

**建议**：补充两个测试：

- `DockEventConsumer.handle()` 遇到未知事件时抛出 `TypeError`
- `UiEventBus.drain()` 能将 consumer 异常通过 `_last_error` 暴露给调用方

```python
def handle(self, event: UiEvent) -> Any:
    match event:
        case CaptureStarted():
            ...
        # ... 所有现有 case ...
        case _:
            raise TypeError(f"Unsupported UI event: {event!r}")
```

### 3. `_tool_nodes` 和 `_agent_nodes` 无清理策略

**问题**：`_tool_nodes` 和 `_agent_nodes` 字典只在 `TurnStarted` 和 `ResetRequested` 时清空。如果单个 turn 内工具调用非常多（如大规模代码搜索），字典会持续增长。

**约束**：不能在 `ToolFinished` 时直接从 `_tool_nodes` 移除条目。现有事件顺序允许 `ToolFinished` 之后再收到 `ToolResultAppended` / `FileChangeAppended`，这些事件仍需要通过 `tool_call_id` 找到 parent node。

**建议**：保留为后续优化。若要实现清理，需要先定义工具节点生命周期，例如在 turn 结束时统一清空，或引入 per-turn 上限并只清理已无后续引用的旧条目。

**权衡**：当前 turn 内工具数量通常不超过几十个，实际内存影响可忽略。此改进优先级低。

### 4. `CompositeEventConsumer.handle_direct()` 的 fire-and-forget 风险

**问题**：`handle_direct()` 对 primary/mirror consumer 的 awaitable result 使用 `asyncio.create_task()` 但不追踪 task，如果异步 consumer 抛出异常，异常会被静默吞掉。

```python
def handle_direct(self, event: UiEvent) -> Any:
    result = self._primary.handle(event)
    if inspect.isawaitable(result):
        asyncio.create_task(result)  # ← 异常丢失
    for mirror in self._mirrors:
        mirror_result = mirror.handle(event)
        if inspect.isawaitable(mirror_result):
            asyncio.create_task(mirror_result)  # ← 异常丢失
    return result
```

**建议**：添加统一 helper 调度 direct task，并记录异常堆栈：

```python
def _schedule_direct_task(self, result: Awaitable[Any], *, target: str) -> None:
    task = asyncio.create_task(result)
    task.add_done_callback(lambda done: self._log_direct_task_error(done, target))

@staticmethod
def _log_direct_task_error(task: asyncio.Task, target: str) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logging.getLogger(__name__).warning(
            "UI event direct %s consumer failed",
            target,
            exc_info=exc,
        )
```

### 5. `UiEventBus` 无背压机制

**问题**：`emit_nowait()` 使用 `put_nowait()`，如果 consumer 处理速度跟不上生产速度，队列会无限增长。

**建议**：设置队列上限（如 1000），`emit_nowait()` 在队列满时丢弃最旧的事件或记录 warning。但这对流式事件（`AssistantStreamUpdated`）需要特殊处理——丢弃中间帧是可接受的（流式文本最终会 commit），但丢弃 `ToolStarted` 等一次性事件不可接受。

**权衡**：当前实际使用中未观察到队列积压问题。此改进优先级低，可作为后续优化。

### 6. 事件 payload 无独立版本号

**现状**：frontend protocol envelope 已经有 `PROTOCOL_VERSION = 1`，并通过 envelope 字段 `v` 发送给前端。WebSocket gateway 发送的是 `UiEventEnvelope`，不是裸事件 payload。

**问题**：`UiEventBase` 本身没有 payload-level 版本号。如果未来支持前端/后端独立部署，单靠 envelope version 可能不足以表达某个事件 payload 的局部兼容变化。

**建议**：本期不改 payload schema。后续若前端独立部署，优先评估是否提升 envelope `PROTOCOL_VERSION`；只有确实需要事件级兼容分支时，再考虑给 `UiEventBase` 增加 payload schema version。

**权衡**：当前前端和后端同步更新，且已有 envelope-level version，此项不紧迫。

## 改进优先级

| 优先级 | 改进项 | 工作量 | 收益 |
|--------|--------|--------|------|
| P0 | 未处理事件 fail-fast 测试覆盖 | 小 | 防止未来回退为静默忽略 |
| P0 | `handle_direct()` primary/mirror 异常日志 | 小 | 提升可观测性 |
| P1 | Todo render constants 注释 | 小 | 降低 grep `TODO` 误读 |
| P2 | 事件 payload 版本策略评估 | 小 | 前后端兼容性 |
| P3 | `_tool_nodes` 清理策略 | 中 | 内存优化（当前不紧迫） |
| P3 | 队列背压机制 | 大 | 防止极端情况下内存溢出 |

## 实现计划

### Phase A（本期）：P0 + P1

> **Implementation status**: Done. `handle_direct()` async task errors are logged, fail-fast behavior is covered by tests, and todo render constants are documented.

#### Step 1: 补充未处理事件 fail-fast 测试

- `tests/test_ui_events.py`: 新增 `test_dock_event_consumer_rejects_unsupported_event`
- `tests/test_ui_events.py`: 新增 `test_ui_event_bus_exposes_consumer_error_on_drain`
- 不改变当前 `case _:` 的 `TypeError` 行为

#### Step 2: 修复 `handle_direct()` 异常丢失

- `src/voidx/ui/output/events/__init__.py`: `CompositeEventConsumer.handle_direct()` 使用 `_schedule_direct_task()` 包装 primary/mirror 的 awaitable result
- `src/voidx/ui/output/events/__init__.py`: `_schedule_direct_task()` 对 task 添加 done callback，并在异常时记录 warning + traceback
- `tests/test_ui_gateway.py` 或 `tests/test_ui_events.py`: 新增 `test_composite_event_consumer_handle_direct_logs_async_mirror_error`

#### Step 3: 注释 Todo render constants

- `src/voidx/ui/output/events/__init__.py`: 在 `TODO_MAX_VISIBLE_ITEMS` 等常量上方添加注释，说明这里的 `TODO` 是 todo tool/render feature 名称，不是待处理代码标记
- 不做 `TASK_*` 重命名，避免和 agent task state 混淆

#### Step 4: 测试

- 验证 `case _:` 对未知事件 fail-fast
- 验证 `UiEventBus.drain()` 暴露 consumer 异常
- 验证 primary/mirror 异常被记录而非静默吞掉

### Phase B（后续）：P2 + P3

- 事件 payload 版本策略评估（优先使用 envelope `v` / `PROTOCOL_VERSION`）
- `_tool_nodes` 清理策略（不能在 `ToolFinished` 立即清理）
- 队列背压机制

## Non-goals

- 不改变事件总线的核心投递语义（单消费者、FIFO、有序）
- 不重构 `DockEventConsumer` 的 match/case 为注册表模式（当前模式清晰且性能足够）
- 不修改 WebSocket gateway 的序列化格式
- 不在本期给 `UiEventBase` 增加 `schema_version`
- 不在 `ToolFinished` 时清理 `_tool_nodes`
- 不实现事件持久化或回放

## 验收标准

- [x] `DockEventConsumer.handle()` 对未处理的事件类型继续 fail-fast
- [x] `UiEventBus.drain()` 能暴露 consumer 处理异常
- [x] `CompositeEventConsumer.handle_direct()` 的 async primary/mirror 异常被记录
- [x] Todo render constants 有明确注释，避免和代码待办标记混淆
- [x] 所有现有测试通过
- [x] 新增测试覆盖 `case _:`、bus error propagation 和 direct mirror 异常日志
