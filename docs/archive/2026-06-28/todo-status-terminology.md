# Todo 状态术语对齐梳理

> **注意：本文档为重构前的历史分析，保留用于追溯决策过程。**
> **状态词表已统一为 `Literal["pending", "active", "done"]`，详见 `docs/archive/todo-status-unification.md`。**
> **下文描述的 4 值词表（`in_progress`/`completed`/`cancelled`）已不再使用。**

## 问题

代码中同时存在 `in_progress` / `active`、`completed` / `done` 等不同命名，容易混淆。
本文档梳理每个术语的出处、含义、以及是否应该统一。

## 权威状态值（唯一真相源）

```
TodoStatus = Literal["pending", "active", "done"]
```

定义位置：`src/voidx/runtime/todo.py:7`
被引用：`TodoRunItem.status`（`task_state.py:50`）、`TodoItem.status`（`tools/todo.py:22`）

**这 3 个值是 todo item 的唯一合法状态。**

## 术语映射表

| 代码中出现的术语 | 出现位置 | 实际含义 | 对应的状态值 |
|-----------------|---------|---------|-------------|
| `in_progress` | TodoStatus, TodoRunItem.status, summary 字符串 | 状态值 | `in_progress` |
| `active` | summary 字符串 `"1 active"` | in_progress 项的计数别名 | `in_progress` |
| `active_items` | TodoRunState 字段 | in_progress 项的子集 | `status == "in_progress"` |
| `completed` | TodoStatus, TodoRunItem.status | 状态值 | `completed` |
| `done` | TodoRunState 字段、summary 字符串 `"/2 done"`、TodoReadFilter | completed 项的计数别名 | `completed` |
| `done` (filter) | TodoReadFilter = `"done"` | 同时包含 completed + cancelled | `completed` ∪ `cancelled` |
| `done` (workflow) | workflow tool action | 工作流节点结束动作，与 todo 无关 | — |
| `done` (transcript) | transcript.py:407 `status="done"` | transcript 节点状态，与 todo 无关 | — |

## 三类"不一致"

### 1. 计数字段名 vs 状态值（可接受）

`TodoRunState` 的计数字段：
```python
done: int          # = count(status == "completed")
in_progress: int   # = count(status == "in_progress")
pending: int       # = count(status == "pending")
cancelled: int     # = count(status == "cancelled")
```

`done` 是 `completed` 的计数别名。这是**有意的缩写**——字段名太长会影响 summary 字符串的可读性（`"2/4 completed"` vs `"2/4 done"`）。

**结论**：可接受，但应在字段 docstring 中注明 `done = completed count`。

### 2. summary 字符串用 "active" 代替 "in_progress"（可接受）

```python
summary = f"{done}/{total} done · {in_progress} active · {pending} pending"
```

`"1 active"` 比 `"1 in_progress"` 更自然。这是面向人类的展示文本，不是状态值。

**结论**：可接受。

### 3. TodoReadFilter 的 "done" 语义不精确（应修复）

```python
TodoReadFilter = Literal["all", "pending", "in_progress", "completed", "done"]
```

`"done"` 过滤器返回 `completed + cancelled`：
```python
if filter_type == "done":
    return {k: v for k, v in todos.items() if v["status"] in ("completed", "cancelled")}
```

而 `"completed"` 过滤器只返回 `completed`。这意味着：
- `filter="completed"` → 只有 completed 项
- `filter="done"` → completed + cancelled 项

`"done"` 是 `"completed"` 的超集，语义模糊——"done" 到底是"完成的"还是"已结束的"？

**结论**：应统一。两个方案：
- **方案 A**：移除 `"done"` filter，让 LLM 用 `"completed"` 或 `"cancelled"` 分别查
- **方案 B**：保留 `"done"` 但改名为 `"finished"`，明确表示"已结束的"（completed + cancelled）

## 跨系统的 "done"（无关项）

以下 `done` 与 todo 状态无关，不需要统一：

| 位置 | 含义 |
|------|------|
| `workflow.py:31` `_WORKFLOW_ACTIONS = ("enter", "advance", "done")` | 工作流节点结束动作 |
| `transcript.py:407` `status="done"` | transcript 节点状态 |
| `tool_executor/workflow.py:294` `transition.get("action") != "done"` | 工作流转换动作 |

## 建议改动

| 优先级 | 改动 | 理由 |
|--------|------|------|
| 低 | TodoRunState 字段加 docstring 注明 `done = completed count` | 减少阅读困惑 |
| 中 | TodoReadFilter 移除 `"done"` 或改名为 `"finished"` | 消除语义歧义 |
| 不改 | summary 字符串的 "active" / "done" | 面向人类，可读性优先 |
| 不改 | `active_items` 字段名 | 语义清晰（活跃项子集），且多处引用 |
