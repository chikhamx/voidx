# UI 事件系统完善度评估与改进设计

> **Status: Draft**

## 背景

UI 事件系统是 voidx 终端渲染的核心管道，负责将 agent 输出（流式文本、工具调用、状态更新等）通过异步事件总线传递到 TUI 渲染层。该系统已经相当成熟，但经过审计发现若干可改进之处。

## 当前架构概览

### 三层结构

```
Schema (schema.py)          Bus (__init__.py)           Consumer (__init__.py)
┌─────────────────┐    ┌──────────────────┐    ┌──────────────────────┐
│ 22 个事件类型    │───▶│ UiEventBus       │───▶│ DockEventConsumer    │
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
| 其他 | `CaptureStarted/Stopped`, `RefreshRequested`, `ResetRequested`, `StartupShown`, `TodoUpdated`, `ErrorAppended`, `WarningAppended`, `FileChangeAppended` | 8 |
| 内部 | `InputSet`, `NoticeSet`, `PermissionToolDetail` | 3 |
| **合计** | | **32** |

## 审计发现

### 1. Todo 渲染常量命名混淆

**问题**：`TODO_MAX_VISIBLE_ITEMS`、`TODO_STATUS_ORDER`、`TODO_ICONS` 中的 `TODO` 容易被误认为是代码中的待办标记（`grep TODO` 会命中），实际是 todo 功能的常量。

**建议**：重命名为 `TASK_MAX_VISIBLE_ITEMS`、`TASK_STATUS_ORDER`、`TASK_ICONS`，或添加模块级注释说明。

**影响范围**：`events/__init__.py` 内部使用，无外部 API 影响。

### 2. `DockEventConsumer` 缺少事件类型覆盖验证

**问题**：`handle()` 方法使用 `match/case` 分发事件，但新增事件类型时没有编译期或运行时检查确保所有事件都被处理。如果新增了 schema 事件但忘记在 consumer 中添加 case，事件会被静默忽略。

**建议**：添加 `case _:` 分支，对未处理的事件记录 warning 日志：

```python
def handle(self, event: UiEvent) -> Any:
    match event:
        case CaptureStarted():
            ...
        # ... 所有现有 case ...
        case _:
            import logging
            logging.getLogger(__name__).warning(
                "Unhandled UI event: %s", type(event).__name__
            )
```

### 3. `_tool_nodes` 和 `_agent_nodes` 无清理策略

**问题**：`_tool_nodes` 和 `_agent_nodes` 字典只在 `TurnStarted` 和 `ResetRequested` 时清空。如果单个 turn 内工具调用非常多（如大规模代码搜索），字典会持续增长。

**建议**：在 `ToolFinished` 时从 `_tool_nodes` 中移除对应条目（除非后续还需要引用）。或者设置上限，超过时清理最旧的条目。

**权衡**：当前 turn 内工具数量通常不超过几十个，实际内存影响可忽略。此改进优先级低。

### 4. `CompositeEventConsumer.handle_direct()` 的 fire-and-forget 风险

**问题**：`handle_direct()` 对 mirror consumer 使用 `asyncio.create_task()` 但不追踪 task，如果 mirror 抛出异常，异常会被静默吞掉。

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

**建议**：添加异常回调：

```python
task = asyncio.create_task(mirror_result)
task.add_done_callback(self._log_mirror_error)

@staticmethod
def _log_mirror_error(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logging.getLogger(__name__).warning("Mirror consumer error: %s", exc)
```

### 5. `UiEventBus` 无背压机制

**问题**：`emit_nowait()` 使用 `put_nowait()`，如果 consumer 处理速度跟不上生产速度，队列会无限增长。

**建议**：设置队列上限（如 1000），`emit_nowait()` 在队列满时丢弃最旧的事件或记录 warning。但这对流式事件（`AssistantStreamUpdated`）需要特殊处理——丢弃中间帧是可接受的（流式文本最终会 commit），但丢弃 `ToolStarted` 等一次性事件不可接受。

**权衡**：当前实际使用中未观察到队列积压问题。此改进优先级低，可作为后续优化。

### 6. 事件 Schema 缺少版本号

**问题**：`UiEventBase` 没有 `version` 字段。WebSocket gateway 将事件序列化发送给前端，如果事件结构变更，前端无法判断兼容性。

**建议**：在 `UiEventBase` 中添加可选的 `schema_version: int = 1` 字段。前端可根据版本号做兼容处理。

**权衡**：当前前端和后端同步更新，版本号暂时不紧迫。但如果未来支持独立部署前端，此字段将变得重要。

## 改进优先级

| 优先级 | 改进项 | 工作量 | 收益 |
|--------|--------|--------|------|
| P0 | `case _:` 未处理事件 warning | 小 | 防止新增事件被静默忽略 |
| P0 | `handle_direct()` mirror 异常日志 | 小 | 提升可观测性 |
| P1 | Todo 常量重命名 | 小 | 消除 grep 误报 |
| P2 | 事件 Schema 版本号 | 小 | 前后端兼容性 |
| P3 | `_tool_nodes` 清理策略 | 中 | 内存优化（当前不紧迫） |
| P3 | 队列背压机制 | 大 | 防止极端情况下内存溢出 |

## 实现计划

### Phase A（本期）：P0 + P1

#### Step 1: 添加 `case _:` 分支

- `src/voidx/ui/output/events/__init__.py`: `DockEventConsumer.handle()` 末尾添加 `case _:` 分支，记录 warning 日志

#### Step 2: 修复 `handle_direct()` 异常丢失

- `src/voidx/ui/output/events/__init__.py`: `CompositeEventConsumer.handle_direct()` 添加 `task.add_done_callback()`

#### Step 3: 重命名 Todo 常量

- `src/voidx/ui/output/events/__init__.py`: `TODO_MAX_VISIBLE_ITEMS` → `TASK_MAX_VISIBLE_ITEMS`，`TODO_STATUS_ORDER` → `TASK_STATUS_ORDER`，`TODO_ICONS` → `TASK_ICONS`
- 更新所有引用处

#### Step 4: 测试

- 验证 `case _:` 对未知事件记录 warning
- 验证 mirror 异常被记录而非静默吞掉
- 验证常量重命名后所有引用正确

### Phase B（后续）：P2 + P3

- 事件 Schema 版本号
- `_tool_nodes` 清理策略
- 队列背压机制

## Non-goals

- 不改变事件总线的核心投递语义（单消费者、FIFO、有序）
- 不重构 `DockEventConsumer` 的 match/case 为注册表模式（当前模式清晰且性能足够）
- 不修改 WebSocket gateway 的序列化格式
- 不实现事件持久化或回放

## 验收标准

- [ ] `DockEventConsumer.handle()` 对未处理的事件类型记录 warning
- [ ] `CompositeEventConsumer.handle_direct()` 的 mirror 异常被记录
- [ ] Todo 相关常量重命名，`grep TODO` 不再误报
- [ ] 所有现有测试通过
- [ ] 新增测试覆盖 `case _:` 和 mirror 异常日志
