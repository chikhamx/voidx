# Workflow Active Scope 收敛方案

> **Status: Done**
> 日期: 2026-06-17
> 状态: 已完成

## 背景

当前 `TaskState.workflow_runs` 是 session runtime snapshot 的一部分，会跨 turn 保留。这个设计支持“继续当前任务”时沿用上一轮 workflow，但也会带来一个问题：

- 同一个 session 中，用户开始了新的目标或普通对话
- 上一轮仍为 `ACTIVE` 的 workflow 没有被清理
- 状态栏继续显示旧 workflow
- Runtime Context 也会把旧 workflow exits 带给模型

这会让新的 turn 继承旧任务的调度约束，表现为“状态面板和模型上下文还挂着上一轮工作流”。

## 目标

1. 新 goal 出现时，不保留上一 goal 的 active workflow
2. general intent turn 清空 workflow 状态
3. 同一个 goal 的继续类 turn 保留 active workflow
4. 避免同一 session 中无意产生多个 active workflow
5. 保持 `workflow_runs` 仍可保存历史 `SATISFIED` / `SKIPPED` / `BLOCKED` 状态，必要时用于审计和快照

## 非目标

- 不重写 workflow DAG
- 不改变 `advance_workflow` 的工具接口
- 不引入并行 workflow 语义
- 不改变状态栏渲染格式
- 不依赖解析 Runtime Context 文本来判断 workflow

## 当前链路

### Goal Resolution

`turn_runner.run_once()` 每轮开始调用 `resolve_goal_for_turn()`，然后：

1. 基于旧 `TaskState` 复制 `turn_task_state`
2. 调用 `turn_task_state.update_after_turn(intent_resolution, user_text, ...)`
3. 调用 `reconcile_workflow_runs_for_turn(...)`
4. 把 `turn_task_state.workflow_runs` 写入 graph state 和 host `_task_state`

### Runtime Context

`RuntimeContextBuilder` 使用：

- `task_state.workflow_route`
- `task_state.workflow_runs`
- `workflow_context.active`

渲染 `Current Task State` 中的：

- `Active workflow nodes`
- `Workflow route`
- `Workflow exits`

### 状态栏

状态栏不解析 Runtime Context 文本。它通过：

```python
active_workflow_names(getattr(self, "_task_state", None))
```

读取 `self._task_state.workflow_runs` 中所有 `status == ACTIVE` 的 workflow 名称。

因此只要 `TaskState.workflow_runs` 收敛正确，状态栏和 Runtime Context 就会同源对齐。

## 设计原则

### Workflow 属于 goal scope

`workflow_runs` 不应被视为 session 永久状态，而应被视为当前 goal 的运行状态。

- goal 未变化：保留 workflow
- goal 变化：清空旧 workflow，再按新 goal 建立 workflow
- intent 变为 general：清空 goal 和 workflow

### 多 active 默认不是目标状态

`workflow_runs` 的数据结构仍允许多个 active，以兼容历史快照和工具 patch；但正常自动调度路径应尽量收敛到一个 active workflow。

如果未来需要并行 workflow，应显式引入并行语义，而不是把多个 active 当作默认行为。

## 行为规则

### 规则 1: general intent 清空 workflow

当 `GoalResolution.intent.type == TaskIntent.GENERAL`：

```python
current_goal = None
workflow_route = None
workflow_runs = {}
```

理由：
- general turn 不应该继承 coding workflow
- 状态栏不应继续显示旧 workflow
- 下一轮如果重新进入 coding，由 resolver 重新选择 workflow

### 规则 2: 新 goal 清空 workflow

当 resolver 返回非空 `resolution.goal`，并且该 goal 与旧 `current_goal` 不同：

```python
current_goal = resolution.goal
workflow_route = route_from_resolution
workflow_runs = {}
```

随后 `reconcile_workflow_runs_for_turn()` 按新的 `plan.join` 激活新 workflow。

Goal 是否相同按结构化字段比较：

- `type`
- `desc`

`GoalSpec.label` 是派生显示值，不作为独立比较字段。

### 规则 3: 同 goal 继续保留 workflow

当 resolver 返回的 goal 与旧 goal 相同，或 resolver 没有返回新 goal 但当前仍是 coding intent：

```python
workflow_runs 保留
workflow_route 按 resolver plan 更新
```

理由：
- 用户说“继续”“接着做”时需要保留当前 workflow
- 长任务不应每轮重新开始 workflow

### 规则 4: plan.join 指向新目标时收敛 active workflow

当 `plan.join` 指向目标 workflow，并且当前存在其他 active workflow：

1. 如果存在 DAG edge：按现有 `advance_workflow_states()` 满足 source 并激活 target
2. 如果是明确 intent override：按现有 `superseded_by_intent` 逻辑满足旧 precursor
3. 如果 target 已经 active，但还存在其他 active：
   - 将其他 active 标记为 `SATISFIED`
   - evidence condition 使用 `superseded_by_active_target`
   - summary 说明 resolver 已选择当前 active target，旧 active 被收敛

这条规则避免当前已有的边界：

```python
brainstorm = ACTIVE
design = ACTIVE
plan.join = "design"
```

继续保留两个 active。

### 规则 5: 无 plan.join 不主动清同 goal workflow

当当前 intent 是 coding，goal 未变化，且 `plan.join` 为空：

```python
workflow_runs 保留
```

理由：
- resolver 可能认为当前 workflow 仍有效，无需重新 join
- “继续”类 turn 应保持当前 active workflow

清理旧 workflow 的职责由规则 1 和规则 2 处理。

## 实现方案

### TaskState.update_after_turn

修改 `src/voidx/runtime/task_state.py`。

新增 helper：

```python
def _same_goal(left: GoalSpec | None, right: GoalSpec | None) -> bool:
    if left is None or right is None:
        return left is right
    return left.type == right.type and left.desc == right.desc
```

更新逻辑：

```python
previous_goal = self.current_goal
self.previous_intent = self.current_intent
self.current_intent = resolution.intent.type

if resolution.intent.type == TaskIntent.GENERAL:
    self.current_goal = None
    self._reset_workflow_context()
    return

if resolution.goal is not None:
    goal_changed = not _same_goal(previous_goal, resolution.goal)
    self.current_goal = resolution.goal
    if goal_changed:
        self._reset_workflow_context()

self.workflow_route = _workflow_route_from_resolution(resolution)
```

注意：`_reset_workflow_context()` 会清空 `workflow_route`，所以 `workflow_route` 需要在 reset 后重新写入。

### reconcile_workflow_runs_for_turn

修改 `src/voidx/workflow/reconcile.py`。

新增收敛规则：

```python
if target and _has_active(runs, target):
    compacted = _satisfy_other_active_runs(
        runs,
        target=target,
        turn_count=turn_count,
    )
    if compacted is not None:
        return compacted
```

建议放在 `_resolve_intent_override()` 之后、`_reconcile_events()` 之前。

新增 helper：

```python
def _satisfy_other_active_runs(
    runs: list[WorkflowRunState],
    *,
    target: str,
    turn_count: int,
) -> list[WorkflowRunState] | None:
    others = [
        run for run in runs
        if run.status == WorkflowRunStatus.ACTIVE and run.name != target
    ]
    if not others:
        return None

    updated = [run.model_copy(deep=True) for run in runs]
    for run in updated:
        if run.status != WorkflowRunStatus.ACTIVE or run.name == target:
            continue
        run.status = WorkflowRunStatus.SATISFIED
        run.updated_turn = turn_count
        run.blocked_reason = ""
        run.evidence.append(WorkflowEvidence(
            kind=WorkflowStateEventKind.SATISFIED.value,
            ref=f"auto:turn_reconcile:{run.name}_superseded_by_active_{target}",
            ok=True,
            summary="Resolver selected an already-active target workflow; stale active workflow was closed.",
            condition="superseded_by_active_target",
        ))
    return updated
```

## 测试策略

### TaskState tests

文件：`tests/test_agent/test_task_state.py`

新增测试：

1. `test_update_after_turn_clears_workflow_for_general_intent`
   - Given: current goal + active workflow
   - When: resolution intent is general
   - Then: current_goal is None, workflow_route is None, workflow_runs is empty

2. `test_update_after_turn_clears_workflow_when_goal_changes`
   - Given: old goal + active workflow
   - When: resolution has different goal and plan.join
   - Then: workflow_runs is empty after update, workflow_route reflects new plan

3. `test_update_after_turn_preserves_workflow_for_same_goal`
   - Given: old goal + active workflow
   - When: resolution returns same goal
   - Then: workflow_runs is preserved

### Workflow reconcile tests

文件：`tests/test_workflow_reconcile.py`

更新现有测试：

- `test_reconcile_does_not_satisfy_source_when_target_is_already_active`

改为期望：

```python
assert by_name["brainstorm"].status == WorkflowRunStatus.SATISFIED
assert by_name["brainstorm"].evidence[-1].condition == "superseded_by_active_target"
assert by_name["design"].status == WorkflowRunStatus.ACTIVE
```

新增测试：

1. `test_reconcile_keeps_single_active_target_when_no_other_active_runs`
   - Given: only design active
   - When: plan.join is design
   - Then: design remains active, no evidence added

2. `test_reconcile_preserves_active_workflow_without_join_for_same_goal`
   - Given: tdd active
   - When: no plan.join
   - Then: tdd remains active

### Run loop tests

文件：`tests/test_agent/test_run_loop.py`

新增或更新测试：

1. 新 goal 不继承旧 active workflow
   - 初始 session state 有 active brainstorm
   - resolver 返回不同 goal + join review
   - 断言最终 `_task_state.workflow_runs` 只有 review active

2. general turn 清空状态栏 workflow 来源
   - 初始 `_task_state.workflow_runs` 有 active tdd
   - resolver 返回 general
   - 断言 `_task_state.workflow_runs == {}`

## 验证命令

Focused:

```bash
.venv/bin/python -m pytest tests/test_agent/test_task_state.py tests/test_workflow_reconcile.py tests/test_agent/test_run_loop.py -q
```

Related:

```bash
.venv/bin/python -m pytest tests/test_agent/test_goal_resolver.py tests/test_agent/test_runtime_context.py tests/test_agent/test_core_flow.py tests/test_tools/test_basic.py -q
```

Full:

```bash
.venv/bin/python -m pytest tests/ -q
git diff --check
```

## 完成标准

- 新 goal 不继承旧 active workflow
- general intent 清空 workflow
- 同 goal 继续保留 workflow
- `plan.join` 指向已 active target 时，其他 active 被收敛
- 状态栏显示与 `TaskState.workflow_runs` 中 active 状态一致
- Runtime Context 的 `Active workflow nodes` 与状态栏同源
- 相关测试和全量测试通过
