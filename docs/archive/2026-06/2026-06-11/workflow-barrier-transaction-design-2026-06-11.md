# Workflow Barrier Transaction — 技术设计文档

> **Status: Done**

## Context

workflow gate 当前在工具授权阶段按 active workflow state 拒绝写类工具。这个约束本身是正确的，但在同一批 tool calls 中同时出现状态变更工具和写工具时，会出现旧状态误杀。

典型场景：

```text
advance_workflow(condition="done", evidence="stale gate cleared")
update(file="docs/specs/subagent-context-isolation-design-2026-06-10.md")
```

当前执行顺序：

1. `GraphToolExecutor.execute_tools()` 收到整批 tool calls。
2. 先调用 `_authorize_tool_calls()` 审核整批工具。
3. `_authorize_tool_calls()` 使用旧 `skill_runs` 计算 `workflow_denied_tools`。
4. `advance_workflow` 被允许，`update/edit/write` 被旧 gate 拒绝。
5. 之后 `advance_workflow` 执行成功并返回 `state_patch`，但同批写工具已经被 deny。

结果是 agent 已经正确判断需要清理 workflow gate，但用户仍看到编辑工具失败。模型需要下一轮重新发起编辑，增加 LLM 轮次，也让用户看到不必要的红叉。

## Goals

- 用户无感：状态推进和后续工具执行在同一个 assistant tool-call 批次内完成。
- 最少 LLM 轮次：不要把 barrier 之后的工具 defer 到下一轮模型调用。
- 权限正确：写工具必须基于 barrier 合并后的最新 state 重新授权。
- 顺序明确：多个 barrier tool 按原始 tool call 顺序串行执行。
- 保留并行能力：状态稳定后，非 barrier 工具仍可按现有规则并行执行。
- 修复 `advance_workflow(done)` 在多 active workflow 下目标不明确的问题。
- 将 `apply_patch` 收敛为 implement-only 工具，避免 orchestrator 用批量 patch 混合文档补充和实现改动。

## Non-Goals

- 不改变 workflow DAG 的语义。
- 不弱化 workflow gate；gate 仍然在授权层强制执行。
- 不改变普通工具的 permission rule。
- 不依赖 prompt 约束 LLM “barrier tool 必须单独调用”。
- 不改变 LangGraph 拓扑。

## Definitions

### Barrier Tool

Barrier tool 是会改变 runtime state，并会影响后续工具授权、可用工具集、workflow gate 或任务状态的工具。

V1 barrier tools：

| Tool | State effect |
|------|--------------|
| `advance_workflow` | 更新 `skill_runs`，可能清除或激活 workflow gate |
| `on_intent` | 更新 `task_intent`、`skill_runs`、`available_tool_ids` |
| `plan_checkpoint` | 更新 `pending_approval`、`task_intent`、`goal` |
| `clarify` | 可能根据用户回答更新 intent 或 pending state |

### Barrier Transaction

Barrier transaction 是 `execute_tools()` 内部的一次状态事务：

```text
execute barrier -> merge state_patch -> rebuild ToolContext -> re-authorize remaining calls
```

它不暴露给 LLM，也不要求用户确认。

### Tool Exposure Boundary

`apply_patch` 是批量 diff 写入工具，可以一次修改多个文件，天然容易混合文档、测试、源码和配置改动。V1 不把它纳入“文档写入例外”的判断，而是在工具集层面收敛：

| Agent | `write` | `edit` | `apply_patch` | Rationale |
|-------|---------|--------|---------------|-----------|
| orchestrator | yes | yes | no | 可做小范围文档补充和单文件精确编辑，但不直接执行批量实现 patch |
| implement | yes | yes | yes | 负责代码实现、重构、跨文件 patch 和验证 |
| explore / plan / review | no | no | no | 保持只读职责 |

这样 workflow gate 只需要处理 `write/edit` 的文档路径例外；`apply_patch` 一律代表实现级写入，必须通过 implement agent 执行。

## Current Behavior

`tool_executor.py` 目前的关键顺序：

```python
approved, denied = await _authorize_tool_calls(..., tool_calls, skill_runs=old_state)
barrier_present = any(_is_barrier_tool(tc) for tc in approved)
if barrier_present:
    approved = barrier_tools_only
    deferred_for_barrier = approved_non_barrier_tools
```

问题有两个：

1. barrier 检测发生在授权之后。被旧 workflow gate 拒绝的写工具不会进入 `deferred_for_barrier`。
2. barrier 执行后的 `state_patch` 只在批次末尾合并，后续工具无法在同批基于新状态授权。

这使得“先清 gate，再编辑”的正确 tool-call 序列仍然失败。

## Proposed Design

### 总体策略

将 `execute_tools()` 改为分阶段处理 tool calls。每个阶段在最新 state 上授权并执行，遇到 barrier 后立即合并 state，再继续处理剩余工具。

高层流程：

```text
pending = original tool calls in order
runtime_state = state snapshot
results = []

while pending:
  prefix_non_barrier, barrier, suffix = split_at_first_barrier(pending)

  if prefix_non_barrier:
    authorize prefix_non_barrier with runtime_state
    execute approved prefix tools using existing parallel rules
    append results and denied messages
    pending = [barrier] + suffix
    continue

  if barrier:
    authorize barrier with runtime_state
    execute barrier serially
    append result
    if barrier failed:
      block suffix with state-barrier-failed messages
      break
    merge state_patch into runtime_state
    rebuild ToolContext from runtime_state
    pending = suffix
    continue

  break
```

这保证：

- barrier 前面的普通工具仍可在旧 state 下正常执行。
- barrier 本身按旧 state 授权，因为它就是用来推进当前 state。
- barrier 后面的工具必须基于新 state 重新授权。
- 同一批 tool calls 内可连续处理多个 barrier。

### 示例

输入：

```text
[
  read("docs/specs/foo.md"),
  advance_workflow(workflow="writing-design-docs", condition="done"),
  update("docs/specs/foo.md")
]
```

执行：

1. `read` 非 barrier，使用旧 state 授权并执行。
2. 遇到 `advance_workflow`，串行执行。
3. 合并 `skill_runs`，`writing-design-docs` 从 active 变为 satisfied。
4. 重建 `ToolContext.active_skill_names` 和 `ToolContext.skill_runs`。
5. 对 `update` 重新授权。若没有 active gate deny edit，则执行。
6. 返回三条 ToolMessage，顺序仍对应原始 tool call 顺序。

## Architecture

### 修改 1：在 `execute_tools()` 内维护 transaction state

最终实现没有新增 `ToolExecutionState` dataclass。`GraphToolExecutor.execute_tools()` 用闭包变量保存 transaction 期间的最新 runtime state：

```python
runtime_task_intent = state.get("task_intent", "chat")
runtime_pending_approval = _dump_pending_approval(state.get("pending_approval"))
runtime_goal = state.get("goal", "")
runtime_skill_runs = _skill_runs_for_state(state.get("skill_runs", []) or [])
state_update: dict = {}
```

同时定义 `make_context()` 从这些闭包变量生成最新 `ToolContext`。每个 barrier 阶段执行后，后续工具都会拿到重建后的 context。

### 修改 2：内联 `apply_state_update()` 并复用现有 patch 合并逻辑

最终实现没有单独抽取 `_patch_from_tool_result()` / `_apply_state_patch()`。它复用已有 `_state_update_from_executed_tools()` 解析工具结果中的 `state_patch`，再通过 `execute_tools()` 内部的 `apply_state_update()` 合并到 transaction state：

```python
apply_state_update(
    _state_update_from_executed_tools(
        segment_executed,
        current_skill_runs=runtime_skill_runs,
    )
)
```

`apply_state_update()` 更新闭包中的 `task_intent`、`pending_approval`、`goal`、`skill_runs`，并立即重建 `ToolContext`。这样避免了新增并行 state 合并路径，也保持了原有 auto-advance 逻辑复用。

### 修改 3：按 barrier 切分执行

新增内部 helper：

```python
def _split_at_first_barrier(tool_calls: list[dict]) -> tuple[list[dict], dict | None, list[dict]]:
    for index, tc in enumerate(tool_calls):
        if _is_barrier_tool(tc):
            return tool_calls[:index], tc, tool_calls[index + 1:]
    return tool_calls, None, []
```

执行策略：

- prefix 非 barrier：走现有 `_authorize_tool_calls()` + `asyncio.gather()` 逻辑。
- barrier：只授权单个 barrier，串行执行。
- suffix：等待 barrier state patch 合并后进入下一轮 loop。

### 修改 4：ToolContext 重建

每次 state patch 合并后必须重建 ToolContext，至少刷新：

- `task_intent`
- `pending_approval`
- `goal`
- `active_skill_names`
- `skill_runs`

否则工具执行时仍可能看到旧状态。

### 修改 5：保留消息顺序

ToolMessage 必须与 assistant 发出的 tool call 一一对应。为避免顺序错乱，最终返回前按原始 tool call 顺序排序：

```python
order = {tc["id"]: index for index, tc in enumerate(original_tool_calls)}
messages.sort(key=lambda msg: order.get(msg.tool_call_id, 10**9))
```

注意：UI 展示可以保持实际执行顺序，因为 barrier transaction 的执行过程对调试有价值；发回 LLM 的 ToolMessage 顺序必须稳定。

### 修改 6：`apply_patch` 只暴露给 implement agent

**文件**: `src/voidx/agent/agents.py`

从 orchestrator 的 tools 列表中移除 `apply_patch`，保留 `write` 和 `edit`：

```python
# orchestrator before
"write", "edit", "apply_patch", "lsp_format",

# orchestrator after
"write", "edit", "lsp_format",
```

implement agent 保留 `apply_patch`：

```python
"read", "write", "edit", "apply_patch", "glob", "grep", "bash", ...
```

同时更新 orchestrator role prompt 中涉及直接编辑的描述，避免提示词继续要求 orchestrator “call write/edit/apply_patch yourself”。新的职责边界：

- orchestrator 可以直接用 `write/edit` 补充 `docs/specs/*.md`、`docs/design/*.md` 等文档。
- orchestrator 遇到跨文件 patch、源码、测试或配置改动时，应委派 implement。
- implement 是唯一可以使用 `apply_patch` 的 agent。

## `advance_workflow` Target Disambiguation

### Problem

当前 `advance_workflow(condition="done")` 选择 active workflow 的排序第一项：

```python
if is_workflow_terminal_condition(condition):
    return active[0]
```

当 active workflows 包含多个 terminal-compatible node 时，`done` 目标不明确。agent 可能连续调用多次 `done` 来“清状态”，导致行为难读，也可能清掉错误节点。

### API Change

给 `AdvanceWorkflowInput` 增加可选字段：

```python
workflow: str = Field(
    default="",
    description="Workflow node to advance. Required when multiple active nodes could match the condition.",
)
```

选择规则：

1. 如果传入 `workflow`，只允许推进该 active workflow。
2. 如果未传 `workflow`：
   - 非 terminal condition：沿用现有逻辑，选择拥有该 outgoing edge 的 active workflow。
   - terminal condition 且只有一个 active workflow：允许。
   - terminal condition 且多个 active workflow：返回 `workflow: ambiguous target`，列出 active workflow names。

### Compatibility

已有调用 `advance_workflow(condition="implemented")` 不受影响，因为 `implemented` 只匹配 `test-driven-development` 的 outgoing edge。

已有调用 `advance_workflow(condition="done")` 在只有一个 active workflow 时不受影响；多个 active workflow 时从“隐式清第一个”改为显式报错，这是更安全的行为。

## Error Handling

| 场景 | 行为 |
|------|------|
| barrier 授权失败 | 返回该 barrier 的 denied ToolMessage；后续工具返回 `Blocked because a prior runtime barrier was denied` |
| barrier 执行失败 | 返回该 barrier 的失败 ToolMessage；后续工具返回 `Blocked because a prior runtime barrier failed` |
| barrier 成功但无 state_patch | 继续执行 suffix，但记录 debug metadata；适用于 clarify 无状态变更 |
| state_patch 无法校验 | barrier 视为失败，阻止后续工具 |
| suffix 工具被新 gate 拒绝 | 正常返回 workflow gate denial，这是正确的新状态结果 |
| 多个 barrier 连续出现 | 按原始顺序逐个执行、逐个合并 state |

## Permissions

权限原则不变：

- barrier tool 也必须经过正常 permission engine。
- barrier 后的工具必须重新授权。
- 不把 barrier 前已经授权的结果复用到 barrier 后。
- `apply_patch` 的第一道约束来自 agent tool exposure：除 implement 外，其他 agent 不应拿到该工具。
- 即使某处绕过工具暴露层，permission/workflow gate 仍应把非 implement 的 `apply_patch` 当作实现级写入拒绝。

这避免出现“旧状态下允许、新状态下不允许”的权限绕过。

## UI Behavior

V1 不新增 UI 概念。用户可见行为：

- 不再看到“先 `advance_workflow` 成功、后 `Update` 被旧 gate 拒绝”的红叉。
- 如果 barrier 后的新状态仍拒绝写工具，用户看到的是准确的新 gate 拒绝原因。
- barrier tool 的展示策略可继续由 tool display policy 独立优化；本设计不依赖隐藏 barrier。

## Tests

### Unit Tests

新增或更新 `tests/test_agent/test_core_flow.py`：

1. `test_advance_workflow_transaction_reauthorizes_following_write`
   - 初始 active `brainstorming`
   - tool calls: `advance_workflow(workflow="brainstorming", condition="done")`, `write(...)`
   - 断言 `write` 不被旧 gate deny
   - 断言 `brainstorming` 最终为 `satisfied`

2. `test_barrier_failure_blocks_following_tools`
   - fake barrier 返回 `metadata={"error": True}`
   - 后续 `write` 不执行
   - 后续 ToolMessage 说明 prior barrier failed

3. `test_multiple_barriers_apply_patches_in_order`
   - `on_intent(implement)` 后接 `advance_workflow(...)` 后接普通工具
   - 断言后续工具看到最终 state

4. `test_non_barrier_prefix_executes_before_barrier`
   - `read`, `advance_workflow`, `write`
   - 断言 `read` 执行，barrier 执行，`write` 基于新 state 执行

5. `test_tool_messages_preserve_original_order`
   - 构造 prefix 并行工具 + barrier + suffix 工具
   - 断言返回 ToolMessage 顺序等于原始 tool call id 顺序

6. `test_orchestrator_does_not_expose_apply_patch`
   - 获取 `get_agent("orchestrator")`
   - 断言 tools 中没有 `apply_patch`
   - 断言 tools 中仍保留 `write` 和 `edit`

7. `test_implement_keeps_apply_patch`
   - 获取 `get_agent("implement")`
   - 断言 tools 中包含 `apply_patch`

新增或更新 `tests/test_tools/test_basic.py`：

8. `test_advance_workflow_done_requires_workflow_when_ambiguous`
   - active `brainstorming` + `writing-design-docs`
   - `condition="done"` 且不传 `workflow`
   - 断言返回 error 和候选列表

9. `test_advance_workflow_done_with_explicit_workflow`
   - active `brainstorming` + `writing-design-docs`
   - `workflow="writing-design-docs", condition="done"`
   - 断言只满足 `writing-design-docs`

### Regression Reproduction

保留一个最小复现测试覆盖截图场景：

```python
parent = AIMessage(tool_calls=[
    {"name": "advance_workflow", "args": {"workflow": "brainstorming", "condition": "done"}, "id": "call_adv"},
    {"name": "write", "args": {"file_path": "tmp-repro.txt", "content": "x"}, "id": "call_write"},
])
```

预期：

- `call_adv` 成功。
- `call_write` 执行或进入正常 permission ask/allow 路径。
- `call_write` 不包含 `Blocked by workflow gate ... brainstorming`。

## Rollout Plan

1. 先实现 `advance_workflow.workflow` 和 ambiguous terminal guard。
2. 从 orchestrator 工具集移除 `apply_patch`，并更新 role prompt 的职责描述。
3. 增加失败测试，锁定当前同批 barrier + write 的旧 gate deny 行为。
4. 重构 `GraphToolExecutor.execute_tools()` 为 barrier transaction loop。
5. 复用现有 state patch 合并逻辑，避免双路径。
6. 跑 focused tests：

```bash
.venv/bin/python -m pytest tests/test_agent/test_core_flow.py tests/test_tools/test_basic.py -v
```

7. 若 focused tests 通过，再跑 workflow 相关测试：

```bash
.venv/bin/python -m pytest tests/test_auto_advance.py tests/test_agent/test_runtime_context.py -v
```

## Acceptance Criteria

- 同一批 tool calls 中 `advance_workflow(done)` 后接写工具，不再被旧 active workflow gate 拒绝。
- barrier 后的写工具使用最新 `skill_runs` 授权。
- barrier 后的新 gate 仍能正确拒绝不允许的工具。
- 多 active workflow 下 `advance_workflow(done)` 不再隐式选择 priority 第一项。
- 所有 ToolMessage 与原始 tool call id 一一对应，顺序稳定。
- orchestrator 不再暴露 `apply_patch`；implement 仍暴露 `apply_patch`。
- 文档补充走 orchestrator 的 `write/edit`；源码、测试、配置和跨文件 patch 走 implement。
- 现有 `on_intent` / `plan_checkpoint` defer 测试更新为 transaction 语义后通过。

## Open Questions

- `clarify` 是否应始终作为 barrier？V1 建议保留，因为它可能根据用户回答返回 `state_patch`。
- barrier 前的普通工具失败是否应阻止后续 barrier？V1 不阻止，保持当前并行工具批处理语义；只有 barrier 自身失败才阻止后续工具。
- UI 是否需要展示 “runtime state updated, continuing tools” 的内部事件？V1 不展示，避免打扰用户。
