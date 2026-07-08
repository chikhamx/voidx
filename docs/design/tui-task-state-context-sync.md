# TUI 状态栏 task_state 跨 context 同步 — 技术设计文档

## Context

TUI 的 `busy_activity_timer` 是一个独立 asyncio task，在 `bind_thread_execution_context` 之外创建。当 timer 触发 `_render_frame` 渲染状态栏时，`host._task_state` property getter 通过 `current_thread_execution_state()` (ContextVar) 获取值——但 timer 的 context 不包含该 ContextVar，所以 getter 回退到 `_default_task_state`。

在 graph 执行过程中（`bind_thread_execution_context` 内），`_default_task_state` 不会被更新（只有 `finally` 块的 `_apply_state` 才同步）。因此 timer 渲染读到的是旧值，导致状态栏的 goal 和 workflow 在 LLM 开始工作后消失。

### 复现路径

1. 用户发消息 → resolver 解析出 goal+workflow → `turn_runner.py:232` 设置 `host._task_state` → `_invalidate_tui` 触发渲染 → 状态栏正确显示
2. `_prepare_with_stream` 设置 `host._task_state` → `_invalidate_tui` 触发渲染 → 状态栏正确显示
3. `_call_llm` 开始执行（`await` LLM 调用）→ timer 每 250ms 触发 `_render_frame` → `host._task_state` getter 返回 `_default_task_state`（旧值，空的）→ 状态栏 goal+workflow 消失

### 关键代码路径

```
app.py:599   asyncio.create_task(submit_result)     ← submit_task，后续进入 bind_thread_execution_context
app.py:600   _start_busy_activity_timer()           ← timer task，在 TUI context 中创建
app.py:274     asyncio.create_task(_busy_activity_timer())

turn_runner.py:107   async with bind_thread_execution_context(host, ...):
turn_runner.py:232     host._task_state = turn_task_state   ← 设置 state.task_state (ContextVar)
llm.py:127             _invalidate_tui(self)                ← call_later 回调在 submit_task context 中执行 ✅

app.py:301     _render_busy_activity_tick() → False → _render_frame()  ← 在 timer context 中执行 ❌
```

## Goals and Non-Goals

### Goals

- 记录根因分析和当前修复方案
- 记录后续优化方向，供未来重构参考

### Non-Goals

- 立即重构 `bind_thread_execution_context` 的 context 传播机制
- 修改 timer 的创建方式（当前修复已足够）

## Architecture

### 当前修复（已实施）

在 `VoidXGraph._task_state` setter 中，当处于 `bind_thread_execution_context` 内时，同时更新 `state.task_state` 和 `_default_task_state`：

```python
# src/voidx/agent/graph/core/voidx_graph.py:152-159
@_task_state.setter
def _task_state(self, value: TaskState) -> None:
    state = current_thread_execution_state()
    if state is not None:
        state.task_state = value
        self._default_task_state = value   # ← 新增：同步到 default
    else:
        self._default_task_state = value
```

**原理**：不是"两套 task_state"，而是"一个 task_state 的两个访问路径"。setter 双写确保无论从哪个 context 读取，都返回最新值。

**测试**：`src/tests/test_agent/test_task_state_context_sync.py` 验证了 context 内设置后、context 外读取的正确性，以及在 `await` 期间外部 task 的可见性。

### 为什么不能让 timer 使用同一个 context

| 约束 | 说明 |
|------|------|
| timer 创建时机 | `app.py:600` 在 `submit_task` 之前创建，此时 `bind_thread_execution_context` 还没进入 |
| timer 生命周期 | 跨越整个 turn（甚至多个 turn），不能绑定到某个 turn 的 context |
| ContextVar 传播 | `asyncio.create_task` 在创建时的 context 中运行；`bind_thread_execution_context` 的 ContextVar 只对进入它的 task 可见 |
| `call_later` 回调 | 在调用时的 context 中执行，但 timer 调用 `invalidate` 时仍在 timer 的 context 中 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| setter 双写 `_default_task_state` | 修改 timer 创建方式使用同一 context | timer 生命周期跨 turn，无法绑定到 turn 级 context |
| setter 双写 `_default_task_state` | 让 timer 不触发完整 `_render_frame`，设置标志等 submit_task 恢复后渲染 | 渲染延迟过大（LLM 调用期间不渲染状态栏变化） |
| setter 双写 `_default_task_state` | 在 `_render_frame` 中跳过 dirty 但不在 context 内的状态栏重算 | 可能漏掉非 task_state 相关的状态栏更新 |

## Open Questions

- [ ] 未来是否可以消除 ContextVar，改用单一数据源（如 `host._task_state` 直接读写，不经过 `ThreadExecutionState`）？这需要重构 `bind_thread_execution_context` 的整个 context 传播机制。
- [ ] `busy_activity_timer` 是否可以改为只做增量更新（`_render_busy_activity_tick`），当行数变化时不直接调用 `_render_frame`，而是通过 `invalidate` 调度？需要验证 `invalidate` 的 `call_later` 回调是否能在 submit_task 的 context 中执行。
- [ ] 其他类似的 ContextVar（如 `_compaction_summary`）是否有同样的跨 context 读取问题？如果 TUI 渲染需要读取这些值，可能需要同样的双写修复。
