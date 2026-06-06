# Todo UI — 结构化进度面板设计

> 让 todo 工具的输出从纯文本升级为 UI 一等公民：结构化事件驱动渲染，实时反映状态变更。

## 1. 背景与目标

当前 `TodoWriteTool.execute()` 返回纯文本进度条 + 图标字符串（`○ ◐ ● ✕`），走的是和 `read`、`write` 一样的 `ToolResult → ToolStarted/ToolFinished` 事件链路。UI 层无法区分 todo 和普通工具调用，无法做进度条动画、状态高亮等。

现状问题：
- todo 输出是手拼 ANSI 风格字符串，没有结构化数据。
- 没有专属 UI 事件类型，todo 变更和普通工具调用一样处理。
- `TaskTracker.set_todos()` 存了结构化数据，但 UI 层从未消费。
- 前端把 todo 当普通 `tool_result` 渲染，无法做专属样式。
- 每次 LLM 调用 todo 工具都是全量替换，没有 diff 语义——但这对 UI 来说足够，因为每次调用就是一次完整快照。

目标：
1. 新增 `TodoUpdated` UI 事件，携带结构化 todo 列表。
2. DockEventConsumer 维护一个 `node_type="todo"` 的 OutputNode，每次 `TodoUpdated` 更新其内容。
3. 前端根据 `node_type="todo"` 做专属渲染（进度条 + 状态列表 + 颜色）。
4. 不改变 todo 工具的调用语义（仍然是全量替换）。
5. 不引入轮询或额外推送机制——LLM 调用 todo 工具本身就是状态变更的时机。

## 2. 核心设计

### 2.1 新增 UI 事件

在 `src/voidx/ui/output/events/schema.py` 中新增：

```python
class TodoUpdated(UiEventBase):
    kind: Literal["todo.updated"] = "todo.updated"
    items: list[TodoItemPayload]
    summary: str  # "3/5 done · 1 active · 1 pending"

class TodoItemPayload(BaseModel):
    model_config = ConfigDict(frozen=True)
    content: str
    status: str  # pending | in_progress | completed | cancelled
```

将 `TodoUpdated` 加入 `UiEvent` union type。

### 2.2 TodoWriteTool 发出事件

在 `src/voidx/tools/todo.py` 的 `execute()` 中，返回 `ToolResult` 之前发出 `TodoUpdated`：

```python
from voidx.ui.output.events import ui_events
from voidx.ui.output.events.schema import TodoUpdated, TodoItemPayload

# 在 execute() 末尾，return 之前
ui_events.emit_direct(TodoUpdated(
    items=[TodoItemPayload(content=t.content, status=t.status) for t in inp.todos],
    summary=f"{done}/{total} done · {in_progress} active · {pending} pending",
))
```

`emit_direct` 是同步的、非阻塞的，不会影响工具执行性能。如果 event bus 未启动（headless 模式），`emit_direct` 返回 `False`，静默忽略。

### 2.3 DockEventConsumer 处理

在 `src/voidx/ui/output/events/__init__.py` 的 `DockEventConsumer.handle()` 中新增 case：

```python
case TodoUpdated() as e:
    self._update_todo_node(e)
```

`_update_todo_node` 逻辑：
1. 在当前 turn 节点下查找或创建 `node_type="todo"` 的 OutputNode。
2. 更新该节点的 `body_lines`（文本渲染）和 `payload`（结构化数据）。
3. 标记节点为 `dirty`，触发 dock 刷新。

```python
def _update_todo_node(self, event: TodoUpdated) -> None:
    if not self._dock.active:
        return
    turn = self._dock.current_turn
    if turn is None:
        return

    # 查找已有的 todo 节点
    todo_node = None
    for child in turn.children:
        if child.node_type == "todo":
            todo_node = child
            break

    if todo_node is None:
        todo_node = OutputNode(
            id=self._dock.tree.next_id(),
            node_type="todo",
            header="Todo",
            header_style="bold",
        )
        turn.add_child(todo_node)

    # 更新结构化 payload
    todo_node.payload = {
        "items": [item.model_dump() for item in event.items],
        "summary": event.summary,
    }

    # 更新文本渲染（fallback for TUI）
    todo_node.header = f"Todo: {event.summary}"
    todo_node.body_lines = self._render_todo_lines(event)

    self._dock.tree.mark_dirty(todo_node.id)
    self._dock.refresh()
```

文本渲染（TUI fallback）复用现有图标风格：

```python
ICONS = {"pending": "○", "in_progress": "◐", "completed": "●", "cancelled": "✕"}

def _render_todo_lines(self, event: TodoUpdated) -> list[str]:
    lines = []
    total = len(event.items)
    done = sum(1 for i in event.items if i.status == "completed")
    if total > 0:
        pct = done / total
        bar_len = 20
        filled = int(bar_len * pct)
        bar = "█" * filled + "░" * (bar_len - filled)
        lines.append(f"[{bar}] {done}/{total} done")
    for status in ["in_progress", "pending", "completed", "cancelled"]:
        items = [i for i in event.items if i.status == status]
        for item in items:
            lines.append(f"  {ICONS[item.status]} {item.content}")
    return lines
```

### 2.4 OutputNode 类型扩展

在 `src/voidx/ui/output/tree.py` 的 `OutputNode.node_type` Literal 中增加 `"todo"`。

在 `src/voidx/ui/transcript.py` 的 `NodeType` 中增加 `"todo"`。

### 2.5 前端渲染

`frontend/src/render.js` 中新增 `node_type === "todo"` 的专属渲染：

```javascript
export function renderTodoNode(node) {
  const el = document.createElement("article");
  el.className = "node node-todo";
  el.dataset.nodeId = node.id;

  // Header with summary
  const header = document.createElement("div");
  header.className = "todo-header";
  header.textContent = node.header;  // "Todo: 3/5 done · 1 active · 1 pending"
  el.append(header);

  // Progress bar
  const items = node.payload?.items || [];
  const total = items.length;
  const done = items.filter(i => i.status === "completed").length;
  if (total > 0) {
    const bar = document.createElement("div");
    bar.className = "todo-progress-bar";
    const fill = document.createElement("div");
    fill.className = "todo-progress-fill";
    fill.style.width = `${(done / total) * 100}%`;
    bar.append(fill);
    el.append(bar);
  }

  // Item list
  const list = document.createElement("ul");
  list.className = "todo-list";
  for (const item of items) {
    const li = document.createElement("li");
    li.className = `todo-item todo-item-${item.status}`;
    li.textContent = `${STATUS_ICON[item.status] || "○"} ${item.content}`;
    list.append(li);
  }
  el.append(list);

  return el;
}
```

在 `renderNodeElement` 中增加分支：

```javascript
if (node.node_type === "todo") {
  return renderTodoNode(node);
}
```

CSS 样式要点：
- `.todo-progress-bar`：圆角灰色背景条
- `.todo-progress-fill`：蓝色填充，`transition: width 0.3s ease` 实现动画
- `.todo-item-in_progress`：蓝色文字 + 旋转图标
- `.todo-item-completed`：绿色文字 + 勾号
- `.todo-item-pending`：灰色文字
- `.todo-item-cancelled`：红色删除线

## 3. 事件流

完整的事件流：

```
LLM 调用 todo 工具
  → TodoWriteTool.execute()
    → tracker.set_todos(items)           # 存储到 TaskTracker
    → ui_events.emit_direct(TodoUpdated) # 发出结构化事件
    → return ToolResult(纯文本)          # 原有返回值不变
  → tool_execution.py
    → ToolStarted 事件                   # 原有流程
    → ToolFinished 事件                  # 原有流程
  → DockEventConsumer
    → handle(ToolStarted)               # 创建 tool_call 节点（原有）
    → handle(TodoUpdated)               # 创建/更新 todo 节点（新增）
    → handle(ToolFinished)              # 完成 tool_call 节点（原有）
```

关键点：`TodoUpdated` 和 `ToolStarted/ToolFinished` 是并行事件。`TodoUpdated` 维护独立的 todo 节点，不替代 tool_call 节点。tool_call 节点仍然存在，记录工具调用历史；todo 节点始终反映最新状态。

## 4. Todo 节点生命周期

- **创建**：第一次 `TodoUpdated` 事件到达时，在当前 turn 下创建 todo 节点。
- **更新**：后续 `TodoUpdated` 事件更新同一个 todo 节点（查找已有 `node_type="todo"` 的子节点）。
- **跨 turn**：每个 turn 有自己的 todo 节点。新 turn 的第一次 `TodoUpdated` 创建新节点。
- **折叠**：todo 节点默认展开。当 turn 结束后，随 turn 节点一起折叠，`collapse_summary` 显示 `"Todo: 3/5 done"`。

## 5. 与现有机制的关系

| 机制 | 关系 |
|------|------|
| `TaskTracker.set_todos()` | 继续使用，是 todo 数据的 source of truth |
| `TodoWriteTool` 返回的 `ToolResult` | 继续返回纯文本，作为 LLM 可见的工具输出 |
| `ToolStarted/ToolFinished` | 继续发出，todo 工具调用仍记录在 tool_call 节点 |
| `TodoUpdated` | 新增，独立于 tool 事件，驱动 UI 结构化渲染 |
| compaction | todo 节点的 `body_lines` 参与 compaction，`payload` 不参与 |

## 6. 改动清单

| 文件 | 改动 |
|------|------|
| `src/voidx/ui/output/events/schema.py` | 新增 `TodoItemPayload`、`TodoUpdated`，加入 `UiEvent` union |
| `src/voidx/tools/todo.py` | `execute()` 中发出 `TodoUpdated` 事件 |
| `src/voidx/ui/output/events/__init__.py` | `DockEventConsumer` 处理 `TodoUpdated`，维护 todo 节点 |
| `src/voidx/ui/output/tree.py` | `OutputNode.node_type` Literal 增加 `"todo"` |
| `src/voidx/ui/transcript.py` | `NodeType` 增加 `"todo"` |
| `frontend/src/render.js` | `renderTodoNode()` 专属渲染 |
| `frontend/styles.css` | todo 进度条和状态样式 |

## 7. 测试策略

单元测试：
- `TodoItemPayload` 序列化/反序列化
- `TodoUpdated` 事件构造和字段验证
- `TodoWriteTool.execute()` 在 event bus 运行时发出 `TodoUpdated`
- `TodoWriteTool.execute()` 在 event bus 未运行时不报错

集成测试：
- `DockEventConsumer` 收到 `TodoUpdated` 后创建 todo 节点
- 连续两次 `TodoUpdated` 更新同一个 todo 节点（不创建新节点）
- todo 节点的 `payload.items` 和 `body_lines` 内容正确
- todo 节点的 `collapse_summary` 显示摘要

前端测试（手动）：
- todo 进度条动画
- 状态图标颜色
- 多次 todo 调用后 UI 正确更新

## 8. 风险与缓解

| 风险 | 缓解 |
|------|------|
| `emit_direct` 在 event bus 未启动时报错 | `emit_direct` 已有 guard，返回 `False` 静默忽略 |
| todo 节点和 tool_call 节点重复显示 | todo 节点是独立节点，tool_call 节点记录调用历史，语义不同 |
| 前端 `render.js` 改动影响现有渲染 | 新增 `node_type === "todo"` 分支，不影响其他 node type |
| `TodoUpdated` 事件频率过高 | 每次 LLM 调用 todo 工具才发一次，频率由 LLM 控制 |
| todo 节点跨 turn 累积 | 每个 turn 独立创建 todo 节点，随 turn 折叠 |
