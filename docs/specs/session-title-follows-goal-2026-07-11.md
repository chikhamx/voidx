# Session Title Follows Goal and Remove User Text Fallback

**Date:** 2026-07-11  
**Status:** Implemented

## TL;DR

每次 turn 解析出 goal 后，都用 `goal.desc` 更新 session title（不仅是首次消息）。同时移除 goal resolver 失败时对原始 user text 的 fallback，改为保留 `current_goal`。所有改变 goal 的路径（turn 结束、`/goal` 命令）都同步更新 session title。

## Context

### 当前行为

**Session title 更新**（`src/voidx/agent/graph/turn_runner.py:373-378`）：

```python
# Update session title to match current goal
goal = intent_resolution.goal
if goal is not None and goal.desc.strip():
    title = goal.desc.strip()
    await update_title(host._session.id, title)
    host._session = host._session.model_copy(update={"title": title})
```

每次 turn 都更新 title，但用的是 `intent_resolution.goal`（turn 开始时 resolver 的输出），不是 turn 结束后的 `final_task_state.current_goal`。graph 执行过程中 workflow 工具可能修改 `current_goal`（`tool_executor/workflow.py:73` → `helpers.py:468-472`），导致 title 与实际 goal 不一致。

**Goal resolver fallback**（`src/voidx/agent/goal_resolver.py:95-98`）：

```python
fallback = GoalResolution(
    intent=IntentResolution(type=TaskIntent.GENERAL),
    goal=GoalSpec(desc=user_text),
    plan=None,
)
```

当 resolver 失败时，goal 退化为原始用户消息。同样的问题在 `_normalize_resolution` 中也存在（第 487、494 行）：`goal = resolution.goal or GoalSpec(desc=user_text)`。

**`/goal` slash 命令**（`src/voidx/agent/slash/handler.py:208-227`）：

```python
if goal.lower() in {"clear", "reset"}:
    task_state.clear_goal()
    ...
    ui.print("[dim]Goal cleared.[/dim]")
    return
...
task_state.set_goal(goal)
...
ui.print(f"[dim]Goal set to [cyan]{goal_label(task_state.current_goal)}[/cyan][/dim]")
```

`/goal <text>` 设置 goal 和 `/goal clear` 清除 goal 后，完全不更新 session title。用户通过 `/goal` 改变了任务目标，但 session title 仍反映旧 goal。

### 问题

1. Session title 不会跟随 goal 变化，用户看到的 title 可能始终是首次消息的摘要
2. 当 resolver 失败时，用原始用户消息作为 goal 不准确（用户消息可能是"继续"、"好的"等无意义内容）
3. `/goal` 命令改变 goal 后不同步 session title，title 与实际 goal 脱节
4. turn 执行过程中 goal 可能被改变（graph 执行时 workflow 工具通过 `tool_executor/workflow.py:73` → `helpers.py:468-472` 修改 `current_goal`），但 title 用的是 turn 开始时的 `intent_resolution.goal`，不是 turn 结束后的 `final_task_state.current_goal`

## Target Behavior

### 1. Session title 跟随 goal（turn 结束时）

每次 turn 结束后，用 `final_task_state.current_goal` 更新 session title：
- 如果 `current_goal` 非空且 `current_goal.desc.strip()` 非空，用 `current_goal.desc.strip()` 更新 session title
- 如果 `current_goal` 为空或 `desc` 为空，不更新 title（保留现有 title）

用 `final_task_state.current_goal` 而非 `intent_resolution.goal`，因为 graph 执行过程中 workflow 工具可能修改 `current_goal`（`tool_executor/workflow.py:73` → `helpers.py:468-472`）。

已移除 `is_first_user_message` 条件限制，当前每次 turn 都执行（无需额外改动）。

### 2. 移除 user_text fallback

在 `goal_resolver.py` 中：

- 初始 `fallback` 改为不设置 goal（或设为 current_goal）
- `_normalize_resolution` 中 `resolution.goal or GoalSpec(desc=user_text)` 改为 `resolution.goal or task_state.current_goal`

当 resolver 失败时：
- 如果 `task_state.current_goal` 存在，保留它
- 如果不存在，goal 保持为空（系统后续会处理空 goal 的情况）

### 3. `/goal` 命令同步 session title

`/goal <text>` 设置 goal 后，用新 goal 的 desc 更新 session title。
`/goal clear` 清除 goal 后，不更新 title（保留现有 title，因为 goal 已为空）。

## Implementation

### File: `src/voidx/agent/graph/turn_runner.py`

位置：约第 373-378 行

改动前：
```python
# Update session title to match current goal
goal = intent_resolution.goal
if goal is not None and goal.desc.strip():
    title = goal.desc.strip()
    await update_title(host._session.id, title)
    host._session = host._session.model_copy(update={"title": title})
```

改动后：
```python
# Update session title to match current goal after turn completes
goal = final_task_state.current_goal
if goal is not None and goal.desc.strip():
    title = goal.desc.strip()
    await update_title(host._session.id, title)
    host._session = host._session.model_copy(update={"title": title})
```

注意：此代码块位于 `final_task_state` 赋值之后（约第 286 行），确保使用 turn 结束后的实际 goal。

### File: `src/voidx/agent/slash/handler.py`

位置：约第 208-227 行，`/goal` 命令处理。

`/goal <text>` 设置 goal 后，追加 title 同步（`# NEW` 标注新增行）：
```python
task_state.set_goal(goal)
self.host.set_task_state(task_state)
self._set_interaction_mode(InteractionMode.GOAL.value)
await self.host.persist_runtime_state()
await self.host.set_session_title(task_state.current_goal.desc)  # NEW
ui.print(f"[dim]Goal set to [cyan]{goal_label(task_state.current_goal)}[/cyan][/dim]")
```

`/goal clear` 不需要额外改动——goal 为空时不更新 title。

### File: `src/voidx/agent/goal_resolver.py`

**改动 1**：第 95-98 行，初始 fallback

改动前：
```python
fallback = GoalResolution(
    intent=IntentResolution(type=TaskIntent.GENERAL),
    goal=GoalSpec(desc=user_text),
    plan=None,
)
```

改动后：
```python
fallback = GoalResolution(
    intent=IntentResolution(type=TaskIntent.GENERAL),
    goal=task_state.current_goal,
    plan=None,
)
```

**改动 2**：第 487 行，GENERAL intent fallback

改动前：
```python
goal = resolution.goal or GoalSpec(desc=user_text)
```

改动后：
```python
goal = resolution.goal or task_state.current_goal
```

**改动 3**：第 494 行，CODING intent fallback

改动前：
```python
goal = resolution.goal or GoalSpec(desc=user_text)
```

改动后：
```python
goal = resolution.goal or task_state.current_goal
```

## Verification

运行相关测试：

```bash
./test.py --backend -- src/tests/test_agent/graph/test_run_loop_title_misc.py
./test.py --backend -- src/tests/test_agent/test_goal_resolver.py
./test.py --backend -- src/tests/test_agent/test_goal_resolver_advanced.py
./test.py --backend -- src/tests/test_llm/test_goal_resolver_retry.py
```

预期：
- 所有现有测试通过
- 如果测试依赖 user_text fallback 行为，需要更新测试
- turn_runner title 更新测试需验证用 `final_task_state.current_goal` 而非 `intent_resolution.goal`
- `/goal` slash 命令测试需验证设置 goal 后 session title 同步更新

## Risks

1. **空 goal**：如果 resolver 失败且没有 current_goal，goal 可能为空。下游代码已确认 None-safe（`voidx_graph.py:560` 用 `goal.label if goal is not None`，`subagent.py:112` 直接赋值）
2. **测试失效**：现有测试可能依赖 user_text fallback 行为，需要检查并更新
3. **Title 频繁更新**：每次 turn 都更新 title 可能导致 title 变化过于频繁，但这是用户期望的行为
4. **`/goal` 命令 title 同步**：`host.set_session_title()` 是 async 方法，需确保在 slash handler 中正确 await
