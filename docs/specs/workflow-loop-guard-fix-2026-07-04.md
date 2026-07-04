# Workflow 工具循环守卫修复 — 技术设计文档

> **Status: Pending**

Date: 2026-07-04

## Context

生产环境观察到 LLM 在 workflow advance 成功后进入无限循环：LLM 反复调用 `workflow(action="advance", workflow="debug", ...)`，但 debug 节点已不再是 active（已 advance 到 tdd）。单次 turn 内产生 49 次 workflow 调用、~6 分钟耗时、733k 累积 input tokens，runtime guard 完全没有拦截。

日志证据（`~/.voidx/logs/agent_events.jsonl`，session `0c71239ad7e6`）：

```
8929: {"event":"hidden_tool_failure","tool_name":"workflow","message":"debug -> nontrivial_fix"}      # advance 成功
8930: {"event":"hidden_tool_failure","tool_name":"workflow","message":"Workflow node 'debug' is not currently active."}  # 第 1 次失败
...
8977: {"event":"hidden_tool_failure","tool_name":"workflow","message":"Workflow node 'debug' is not currently active."}  # 第 48 次失败
```

全局统计：2790 次 workflow `hidden_tool_failure`，涉及 `debug`、`design`、`tdd` 等多个节点。

## Goals and Non-Goals

### Goals

1. workflow guidance（`applied: false`）不计为 progress，也不生成新的 evidence，重复 guidance 调用能触发 NoProgress terminate。
2. `todo` 和 `workflow` 的 tool calls / ToolMessage 都保留在 LLM 上下文中，不再被 replay sanitize 移除。
3. workflow runtime 维护单 active node 语义；guidance 消息明确告知 `current_node` 和可用 exit，减少 LLM 误用旧 workflow 名的概率。
4. `LOW_VALUE_REPETITIVE_TOOL_KEYS` 覆盖 workflow guidance 场景，`is_stuck` 能拦截重复调用。
5. 测试覆盖以上所有修复点。

### Non-Goals

- 不改变 workflow advance 成功后 `should_continue` 的行为（仍允许 LLM 在新节点继续工作）。
- 不改变 workflow 工具的 HIDDEN display mode。
- 不修改 compaction 的 `preserve_trailing_ai_tool_calls` 逻辑。
- 不修改 `_explicit_advance_route_limited_runs` 的 route 终止判断。

## Current State

### 失败 1：todo/workflow ToolMessage 被 replay sanitize 移除

**文件**：`src/voidx/agent/todo_state.py`

```python
# line 18-21
_REPLAY_SANITIZED_TOOL_NAMES = frozenset({
    "todo",
    "workflow",
})
```

`todo` 和 `workflow` 在 replay 时都会被移除。原始假设是 todo 状态通过 `AgentState.todo_state` 流转，不依赖 ToolMessage；但实际调试时，保留 todo / workflow 的完整 tool exchange 更有利于 LLM 理解上一轮状态变化，且这两个工具的消息体较小，长期保留的 token 成本可接受。

```python
# line 243-259  _latest_todo_tool_call_ids
def _latest_todo_tool_call_ids(messages: list[BaseMessage]) -> set[str]:
    index = len(messages) - 1
    while index >= 0:
        message = messages[index]
        if isinstance(message, AIMessage):
            preserved: set[str] = set()
            for call in getattr(message, "tool_calls", None) or []:
                if not isinstance(call, dict):
                    continue
                if call.get("name") == "todo":          # ← 只保留最近一轮 todo
                    call_id = str(call.get("id") or "")
                    if call_id:
                        preserved.add(call_id)
            if preserved:
                return preserved
        index -= 1
    return set()
```

`preserve_latest_tool_exchange=True` 只保留最新的 **todo** 调用，历史 todo 和所有 workflow 都会被 sanitize。

**调用路径**：`src/voidx/agent/graph/core/llm.py:183`

```python
state_messages = sanitize_todo_replay_messages(
    list(state["messages"]),
    preserve_latest_tool_exchange=True,
)
```

每次 `_call_llm` 都执行此 sanitize，移除历史 todo/workflow ToolMessage。LLM 在 advance 成功后的下一轮调用时，看不到 advance 成功的 ToolMessage，也看不到 guidance 消息；同时也看不到更早 todo 状态更新的原始工具结果。

**验证**（模拟测试）：

```
Original: 多轮 HumanMessage, AIMessage[todo/workflow tool_calls], ToolMessage[result]
Sanitized: 只保留非 runtime 工具，以及最多最近一轮 todo exchange
```

todo/workflow 的历史调用和结果被移除，部分 AIMessage 的 tool_calls 被清空。

### 失败 2：`cycle_summary_from_tools` 误判 guidance 为"有进展"

**文件**：`src/voidx/agent/graph/runtime_guards.py:429-432`

```python
# Non-read-only todo/workflow calls always count as progress.
key = tool_op_key(tool_call)
if key in ("todo:write", "todo:update") or key.startswith("workflow:"):
    has_progress = True
```

`key.startswith("workflow:")` 无条件设 `has_progress=True`，不检查结果是否是 guidance（`applied: false`）。

**NoProgressState**（`runtime_guards.py:211-217`）：

```python
def record_cycle(self, summary: ToolCycleSummary) -> GuardGuidance | None:
    unseen_evidence = [key for key in summary.evidence_keys if key not in self.seen_evidence_keys]
    if summary.has_progress or unseen_evidence:   # ← has_progress=True 直接重置
        self.consecutive = 0
        self.warned = False
        self.seen_evidence_keys.update(summary.evidence_keys)
        return None
```

`has_progress=True` 导致计数器永远归零，48 次失败从未触发 NoProgress terminate（阈值 5）。

### 失败 3：`is_stuck` 不拦截 workflow 重复调用

**文件**：`src/voidx/agent/graph/runtime_guards.py:14, 166-176`

```python
LOW_VALUE_REPETITIVE_TOOL_KEYS = frozenset({"todo:read", "checkpoint"})  # ← 不含 workflow:advance

def is_stuck(self) -> tuple[bool, str, int]:
    ...
    if all(item.only_tool == tool for item in window):
        if tool in LOW_VALUE_REPETITIVE_TOOL_KEYS and not any(item.has_progress for item in window):
            return True, tool, len(window)
    return False, "", 0
```

`workflow:advance` 不在 `LOW_VALUE_REPETITIVE_TOOL_KEYS` 里，即使连续 5 次相同调用也不触发 `is_stuck`。

### guidance 消息内容

**文件**：`src/voidx/tools/workflow.py:296-309, 345-351`

guidance 返回 `metadata={"workflow_guidance": payload}`，payload 当前包含多 active 语义字段：

```json
{
  "action": "advance",
  "applied": false,
  "reason": "invalid_active_workflow",
  "guidance": "Workflow node 'debug' is not currently active.",
  "active_nodes": ["tdd"],
  "suggested_call": "workflow(action=\"advance\", condition=\"...\", evidence=\"...\")"
}
```

`suggested_call` 不带 `workflow` 参数（因为不传 workflow 时会自动选择 active node）。但 LLM 看不到这个 guidance（被 sanitize 移除），且 `active_nodes` 暗示 runtime 允许多个 active node。设计上应收敛为单 active node，并在 payload 中暴露 `current_node`。

### 区分 guidance 和 success

**文件**：`src/voidx/tools/workflow.py`

| 函数 | metadata | 含 state_patch |
|------|----------|----------------|
| `_success()` (line 274) | `{"workflow_transition": ..., "state_patch": ...}` | ✅ |
| `_guidance()` (line 296) | `{"workflow_guidance": payload}` | ❌ |

`_tool_result_ok()`（`tool_executor/types.py:25`）对两者都返回 True（无 error/blocked/timeout），所以 guidance 的 ToolMessage status 是 `"success"`，不会被 `sanitize_failed_tool_exchanges` 移除。

## Design

### 修复 1：todo/workflow ToolMessage 保留在 LLM 上下文

**文件**：`src/voidx/agent/todo_state.py`

不再对 `todo` / `workflow` 做 replay sanitize。最小实现可以先将 `_REPLAY_SANITIZED_TOOL_NAMES` 改为空集合；如果后续确认没有其他调用方依赖，也可以删除相关保留最近 todo exchange 的特殊逻辑。

```python
# Before
_REPLAY_SANITIZED_TOOL_NAMES = frozenset({
    "todo",
    "workflow",
})

# After
_REPLAY_SANITIZED_TOOL_NAMES = frozenset()
```

**影响范围**：

- `_REPLAY_SANITIZED_TOOL_PATTERN`（line 22）需要能处理空集合，避免生成无效 regex
- `_DSML_RUNTIME_INVOKE_RE`（line 23-26）在空集合时应禁用或不匹配任何工具
- `_sanitize_ai_runtime_calls`（line 137）不再移除 todo/workflow tool_calls
- `_latest_todo_tool_call_ids`（line 243）不再参与 replay 保留逻辑，可删除或保留为未使用兼容 helper
- `_trailing_ai_runtime_tool_call_ids`（line 262）不再需要为 todo/workflow 做特殊保留

**同步更新**：`src/voidx/ui/output/display_policy.py:113,118`

```python
# Before
"todo": ToolDisplayRule(tool_name="todo", mode=ToolDisplayMode.HIDDEN, replay_sanitize=True),
"workflow": ToolDisplayRule(tool_name="workflow", mode=ToolDisplayMode.HIDDEN, replay_sanitize=True),

# After
"todo": ToolDisplayRule(tool_name="todo", mode=ToolDisplayMode.HIDDEN),
"workflow": ToolDisplayRule(tool_name="workflow", mode=ToolDisplayMode.HIDDEN),
```

同步更新 display policy 注释，避免继续声明 todo/workflow 会 suppress ToolMessage on replay。

**效果**：todo/workflow ToolMessage 都保留在 LLM 上下文中，LLM 能看到上一轮 todo 状态更新、workflow advance 成功结果或 guidance 消息。

### 修复 2：guidance 不计为 progress，也不生成 evidence

**文件**：`src/voidx/agent/graph/runtime_guards.py:429-437`

```python
# Before
key = tool_op_key(tool_call)
if key in ("todo:write", "todo:update") or key.startswith("workflow:"):
    has_progress = True
# Evidence: only skip low-value keys (todo:read, checkpoint).
if key and key not in LOW_VALUE_REPETITIVE_TOOL_KEYS and ok(result):
    evidence_key = _tool_evidence_key(tool_call, result)
    if evidence_key:
        evidence_keys.append(evidence_key)

# After
key = tool_op_key(tool_call)
metadata = getattr(result, "metadata", {}) or {}
is_workflow_guidance = "workflow_guidance" in metadata
if key in ("todo:write", "todo:update") or (key.startswith("workflow:") and not is_workflow_guidance):
    has_progress = True
if is_workflow_guidance:
    continue
if key and key not in LOW_VALUE_REPETITIVE_TOOL_KEYS and ok(result):
    evidence_key = _tool_evidence_key(tool_call, result)
    if evidence_key:
        evidence_keys.append(evidence_key)
```

**判断依据**：`_guidance()` 返回 `metadata={"workflow_guidance": payload}`，`_success()` 返回 `metadata={"workflow_transition": ..., "state_patch": ...}`。检查 `"workflow_guidance" in metadata` 即可区分。

**效果**：workflow guidance 不再重置 NoProgress 计数器，也不会通过 unseen `evidence_keys` 间接重置 NoProgress；连续 5 次 guidance 调用会触发 NoProgress terminate。

### 修复 3：`LOW_VALUE_REPETITIVE_TOOL_KEYS` 覆盖 workflow

**文件**：`src/voidx/agent/graph/runtime_guards.py:14`

```python
# Before
LOW_VALUE_REPETITIVE_TOOL_KEYS = frozenset({"todo:read", "checkpoint"})

# After
LOW_VALUE_REPETITIVE_TOOL_KEYS = frozenset({"todo:read", "checkpoint", "workflow:advance", "workflow:enter", "workflow:done"})
```

**效果**：`is_stuck()` 能拦截连续 5 次相同的 workflow 调用（即使每次都返回 guidance）。

**注意**：`is_stuck` 还检查 `not any(item.has_progress for item in window)`。修复 2 之后 guidance 不计为 progress，所以 `has_progress=False`，`is_stuck` 能正常触发。

### 修复 4：guidance 消息增强并收敛为单 active node

**文件**：`src/voidx/tools/workflow.py:345-351`

在 `invalid_active_workflow` guidance 中用 `current_node` 表达当前唯一 active node；不再使用 `active_nodes` / `current_active` 这类多节点或含糊字段。

```python
# Before
return None, None, _guidance(
    action="advance",
    reason="invalid_active_workflow",
    guidance=f"Workflow node {requested!r} is not currently active.",
    active_nodes=[run.name for run in active],
    suggested_call=_suggested_advance_call(active),
)

# After
current_node = active[0].name if active else ""
return None, None, _guidance(
    action="advance",
    reason="invalid_active_workflow",
    guidance=(
        f"Workflow node {requested!r} is not currently active. "
        f"Current node: {current_node}. "
        "Omit the workflow parameter to use the current node."
    ),
    current_node=current_node,
    suggested_call=_suggested_advance_call(active),
)
```

**单 active invariant**：workflow runtime 应维护最多一个 active node。若 `_active_runs()` 或调用入口发现多个 active node，应在进入 guidance/advance 前规范化为一个明确节点，或返回明确的 runtime/state error；不应继续向 LLM 暴露多个 active candidate。

**效果**：即使 LLM 只看到 guidance 文本（`result.summary`），也能明确知道当前节点，并避免继续使用已过期的 `workflow="debug"` 参数。

## File Structure

| 文件 | 修改内容 |
|------|----------|
| `src/voidx/agent/todo_state.py` | 不再 replay sanitize `todo` / `workflow`；确保空 `_REPLAY_SANITIZED_TOOL_NAMES` 不生成误匹配 regex |
| `src/voidx/ui/output/display_policy.py` | 移除 `todo` / `workflow` 的 `replay_sanitize=True`，同步更新注释 |
| `src/voidx/agent/graph/runtime_guards.py` | workflow guidance 不计 progress、不生成 evidence；`LOW_VALUE_REPETITIVE_TOOL_KEYS` 覆盖 workflow guidance 场景 |
| `src/voidx/tools/workflow.py` | `invalid_active_workflow` guidance 使用 `current_node`，并维护单 active node 语义 |
| `tests/test_agent/test_guards_tool_op.py` | 更新 workflow guidance progress/evidence、low-value key、`is_stuck` 覆盖 |
| `tests/test_agent/test_todo_replay_sanitization.py` | 更新为多轮 todo/workflow tool_calls 与 ToolMessage 均保留 |
| `tests/test_tools/test_workflow_tool.py` | 新增 `current_node` guidance 消息内容测试 |

## Tests

### 测试 1：workflow guidance 不计为 progress 且不生成 evidence

```bash
./python.sh -m pytest tests/test_agent/test_guards_tool_op.py -k "workflow_guidance_no_progress" -v
```

验证：`cycle_summary_from_tools` 对 workflow guidance 结果返回 `has_progress=False`，且 `evidence_keys == []`。

### 测试 2：重复 workflow guidance 触发 NoProgress terminate

```bash
./python.sh -m pytest tests/test_agent/test_runtime_guards.py -k "workflow_guidance_terminate" -v
```

验证：连续 5 次 workflow guidance 调用后 `NoProgressState.decision().action == "terminate"`。

### 测试 3：todo/workflow ToolMessage 全量保留在 replay 中

```bash
./python.sh -m pytest tests/test_agent/test_todo_replay_sanitization.py -k "runtime_tool_messages_preserved" -v
```

验证：`sanitize_todo_replay_messages` 不移除多轮 todo/workflow tool_calls 和 ToolMessage。

### 测试 4：workflow success 仍计为 progress

```bash
./python.sh -m pytest tests/test_agent/test_guards_tool_op.py -k "workflow_success_progress" -v
```

验证：`cycle_summary_from_tools` 对 workflow advance 成功结果返回 `has_progress=True`。

### 测试 5：guidance 消息包含 current_node 信息

```bash
./python.sh -m pytest tests/test_tools/test_workflow_tool.py -k "guidance_current_node" -v
```

验证：`invalid_active_workflow` guidance 的 payload 包含 `current_node`，文本中包含当前节点名称，且不再返回 `active_nodes`。

### 测试 6：is_stuck 拦截重复 workflow guidance

```bash
./python.sh -m pytest tests/test_agent/test_guards_tool_op.py -k "workflow_guidance_is_stuck" -v
```

验证：连续相同 `workflow:advance` guidance 且无 progress 时，`is_stuck()` 返回 True。

## Risks

1. **上下文 token 增长**：保留 todo/workflow ToolMessage 会增加 LLM 上下文 token。但这两个工具结果体积较小，换取状态可见性和循环恢复能力；compaction 仍会在 context 溢出时触发。

2. **空 sanitize 集合兼容性**：`_REPLAY_SANITIZED_TOOL_NAMES = frozenset()` 后，regex 构造不能产生空 alternation 的误匹配；实现需显式禁用 `_DSML_RUNTIME_INVOKE_RE` 或让其不匹配任何工具。

3. **display_policy 同步**：`replay_sanitize` 标志与 `_REPLAY_SANITIZED_TOOL_NAMES` 需保持同步。修改后需确认 `display_policy.py` 不再声明 todo/workflow 会 suppress ToolMessage on replay。

4. **`is_stuck` 误判**：workflow guidance 加入低价值重复判断后，如果 LLM 合理地连续 advance 成功，不应被误判；`is_stuck` 仍要求 `not any(item.has_progress)`，而 workflow success 会继续计为 progress。

5. **单 active node 迁移**：现有代码和测试可能仍构造多个 active runs。实现需要决定是规范化为一个 active node，还是返回明确错误；测试需覆盖多 active 输入不会继续暴露 `active_nodes`。

6. **现有 sanitize 测试**：当前 `_latest_todo_tool_call_ids` 相关测试会过时。应改测新的 replay 行为，或删除不再使用的 helper 测试。
