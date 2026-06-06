> **Status: Done**

# Todo UI — TUI 结构化进度面板设计

> 让 todo 工具的状态从纯文本工具输出升级为 TUI 输出树中的结构化进度节点。

## 1. 背景与目标

当前 `TodoWriteTool.execute()` 返回纯文本进度条和状态图标字符串（`○ ◐ ● ✕`）。工具本身已经通过 `TaskTracker.set_todos()` 保存结构化 todo 数据，但 TUI 只能把 todo 当普通工具调用看待，无法稳定显示“当前任务列表”。

本轮只做 TUI：
- 不做 web/frontend 渲染。
- 不做前端进度条动画。
- 不支持 TUI browse 折叠 todo 节点。
- 支持 TUI 面板最大显示行数，避免大型 todo 列表刷屏。

目标：
1. 新增 `TodoUpdated` UI 事件，携带结构化 todo 快照。
2. `TodoWriteTool` 继续保持工具语义：每次调用全量替换 todo 列表，并返回原有纯文本 `ToolResult`。
3. 工具执行层在 todo tool 成功返回后发出 `TodoUpdated`，避免 `src/voidx/tools` 依赖 UI 层。
4. `DockEventConsumer` 在当前 assistant/subagent 节点下维护一个 `node_type="todo"` 的 `OutputNode`。
5. TUI fallback 使用 `body_lines` 渲染进度条和状态列表，超过最大行数时显示省略提示。

## 2. 核心设计

### 2.1 UI 事件

在 `src/voidx/ui/output/events/schema.py` 中新增：

```python
TodoStatus = Literal["pending", "in_progress", "completed", "cancelled"]

class TodoItemPayload(BaseModel):
    model_config = ConfigDict(frozen=True)
    content: str
    status: TodoStatus

class TodoUpdated(UiEventBase):
    kind: Literal["todo.updated"] = "todo.updated"
    items: list[TodoItemPayload]
    summary: str
```

将 `TodoUpdated` 加入 `UiEvent` union type。

`agent_id` 使用 `UiEventBase.agent_id`：
- `-1` 表示顶层 assistant。
- `>= 0` 表示 child agent，沿用现有 subagent 事件归属规则。

### 2.2 Todo tool 输出结构化 metadata

`TodoWriteTool` 不直接 import UI 事件。它继续返回纯文本 `output`，同时在 `metadata` 中增加：

```python
metadata={
    "total": total,
    "done": done,
    "in_progress": in_progress,
    "pending": pending,
    "cancelled": cancelled,
    "todo_items": [item.model_dump(mode="json") for item in inp.todos],
    "todo_summary": f"{done}/{total} done · {in_progress} active · {pending} pending",
}
```

同时将 `TodoItem.status` 收紧为同一套 `TodoStatus`，避免无效状态进入 TUI 渲染。

### 2.3 工具执行层发事件

顶层工具执行路径在 `GraphToolExecutionMixin._execute_tools()` 中处理：

1. `ToolStarted` 已经先于工具执行发出。
2. `self.tools.execute_tool(...)` 返回 `ToolResult`。
3. 如果 `tid == "todo"` 且 metadata 中存在 `todo_items`，发出 queued `TodoUpdated`。
4. 再发出 `ToolFinished`。

使用 queued `await ui_events.emit(...)`，保证和 `ToolStarted` / `ToolFinished` 在同一个 UI event bus 顺序中处理。

child agent 工具执行路径在 `src/voidx/agent/graph/subagent.py` 中处理。它已有 `agent_id`，所以发出：

```python
TodoUpdated(agent_id=agent_id, items=..., summary=...)
```

注意：subagent 使用 `ui_events.emit_direct(todo_event)` 而非 queued `emit`。因为 subagent 在独立的 asyncio 任务中运行，不共享主事件队列的 drain 循环，`emit_direct` 直接将事件应用到 consumer，保证 todo 更新在 subagent 上下文中即时可见。

### 2.4 DockEventConsumer 处理

在 `src/voidx/ui/output/events/__init__.py` 中新增 `TodoUpdated` case。

父节点选择：
- `event.agent_id >= 0`：使用 `_agent_parent(event.agent_id)`，和现有 subagent tool/stream 事件保持一致。
- 顶层：使用当前 assistant 节点；如果还不存在，则 `ensure_agent()`。

节点维护：
- 在父节点子节点中查找已有 `node_type == "todo"` 的节点。
- 找不到则通过 `self._dock.tree.new_node(parent=parent, node_type="todo", ...)` 创建。
- 更新 `payload`、`header`、`body_lines`。
- `status` 固定为 `"done"`，并标记 node settled；todo 面板是状态快照，不是长运行节点。

### 2.5 TUI 文本渲染

todo 节点 header：

```text
Todo: 3/5 done · 1 active · 1 pending
```

body_lines：
1. 第一行是 20 格进度条。
2. 后续按 `in_progress`、`pending`、`completed`、`cancelled` 分组。
3. 最多显示 `TODO_MAX_VISIBLE_ITEMS` 条任务。
4. 超出时追加省略行，例如：

```text
[████████░░░░░░░░░░░░] 2/5 done
  ◐ implement event
  ○ add tests
  ● update docs
  … 2 more todos
```

任务内容写入 `body_lines` 前使用 Rich `escape()`，避免 todo 文本被当成 Rich markup。

## 3. 事件流

```text
LLM 调用 todo 工具
  -> tool_execution.py
    -> ToolStarted
    -> TodoWriteTool.execute()
      -> tracker.set_todos(items)
      -> return ToolResult(output=纯文本, metadata=结构化 todo 快照)
    -> TodoUpdated
    -> ToolFinished
  -> DockEventConsumer
    -> handle(ToolStarted)   创建/更新 tool_call 节点
    -> handle(TodoUpdated)   创建/更新 todo 面板节点
    -> handle(ToolFinished)  完成 tool_call 节点
```

`TodoUpdated` 不替代 `ToolStarted/ToolFinished`。tool_call 节点仍记录工具调用历史，todo 节点显示当前任务列表快照。

## 4. 节点生命周期

- 每个 assistant/subagent 父节点最多一个 todo 节点。
- 同一父节点后续 `TodoUpdated` 更新原节点，不创建重复节点。
- 新一轮 assistant 输出会有新的 assistant 父节点，因此 todo 面板也自然分轮。
- 不支持 browse 折叠 todo 节点；通过最大显示行数控制高度。

## 5. 改动清单

| 文件 | 改动 |
|------|------|
| `src/voidx/ui/output/events/schema.py` | 新增 `TodoStatus`、`TodoItemPayload`、`TodoUpdated`，加入 `UiEvent` union |
| `src/voidx/tools/todo.py` | 收紧 status 类型，返回结构化 todo metadata |
| `src/voidx/agent/graph/tool_execution.py` | 顶层 todo tool 返回后发出 `TodoUpdated` |
| `src/voidx/agent/graph/subagent.py` | child agent todo tool 返回后发出带 `agent_id` 的 `TodoUpdated` |
| `src/voidx/ui/output/events/__init__.py` | `DockEventConsumer` 处理 `TodoUpdated`，维护 todo 节点 |
| `src/voidx/ui/output/tree.py` | `OutputNode.node_type` Literal 增加 `"todo"` |
| `src/voidx/ui/transcript.py` | `NodeType` 增加 `"todo"`，保证 transcript round-trip |

## 6. 测试策略

单元测试：
- `TodoUpdated` 事件构造和 status 字段验证。
- `TodoWriteTool.execute()` 返回 `todo_items` / `todo_summary` metadata。
- 无效 todo status 被 Pydantic validation 拒绝。

UI 事件测试：
- `DockEventConsumer` 收到 `TodoUpdated` 后在 assistant 下创建 todo 节点。
- 连续两次 `TodoUpdated` 更新同一个 todo 节点。
- `agent_id >= 0` 的 `TodoUpdated` 挂到对应 subagent 节点下。
- todo 文本渲染最大显示行数和省略提示正确。

工具执行测试：
- 顶层 todo tool 执行后发出 `TodoUpdated`。
- child agent todo tool 执行后发出带 `agent_id` 的 `TodoUpdated`。
