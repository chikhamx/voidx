# Todo Context — IDs Only in Current Task State

> **Status: Done (with deviation)** — 已在 `runtime_context.py:278-287` 实现，
> 但实现偏离原设计：原设计要求只输出 active+pending 项的 id 列表，实际实现
> 输出 `summary` + active 项 content（截断至 60 字符）。测试见
> `tests/test_agent/test_runtime_context_builder.py`。该偏离是后续演进的
> 结果，保留本文档用于追溯原始设计意图。

## Context

Current Task State 的 todo 段落当前只显示 `active_items`（in_progress 项）的完整信息（id + status + content）。
这会占用系统提示的 token，且 LLM 并不需要每次都看到 content——id 本身是语义化的（如 'schema', 'api'）。

目标：只放 active + pending 项的 id 列表，LLM 需要详情时调用 `todo` 工具的 `read` 操作查看。

## 改动

### `src/voidx/agent/runtime_context.py` — `_current_task_state()` (L276-280)

**Before:**
```python
if todo_state is not None and todo_state.active_items:
    lines.append(f"- Active todo: {todo_state.summary}")
    for item in todo_state.active_items:
        lines.append(f"  - [{item.id}] {item.status}: {item.content}")
```

**After:**
```python
if todo_state is not None and todo_state.items:
    visible = [i for i in todo_state.items if i.status in ("active", "pending")]
    if visible:
        ids = ", ".join(i.id for i in visible)
        lines.append(f"- Todo: {todo_state.summary}")
        lines.append(f"  Active/Pending: {ids}")
        lines.append("  Call todo with op=read for details.")
```

变化点：
- 用 `todo_state.items`（全部项）替代 `todo_state.active_items`（仅 in_progress）
- 筛选 `in_progress` + `pending` 状态
- 只输出 id，不含 status 和 content
- 标题从 "Active todo" 改为 "Todo"
- 末尾加提示语

### 测试更新

| 文件 | 测试 | 改动 |
|------|------|------|
| `test_runtime_context_builder.py:149-150` | 断言 "Active todo: ..." 和 "- [ctx] in_progress: ..." | 改为断言 "Todo: ..." 和 "Active/Pending: ctx" |
| `test_call_llm_compaction.py:113-118` | 断言 "Active todo" 和 "- [todo_replay] in_progress: ..." | 改为断言 "Todo" 和 "Active/Pending: todo_replay" |

## Non-Goals

- 不改 UI 渲染（TUI/dock 已在上一轮修复为显示全部项）
- 不改 `active_items` 字段本身（runtime_context 之外的地方仍在用）
- 不改 todo 工具的 `read` 操作（已支持 filter）
