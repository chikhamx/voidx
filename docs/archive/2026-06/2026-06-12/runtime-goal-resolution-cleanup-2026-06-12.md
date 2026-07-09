# Runtime Goal Resolution Cleanup — 技术设计文档

## Context

现有 goal/workflow/persona 重构已经拆出了 `TaskIntent`、`Goal`、hidden persona
和 runtime state 边界，但实现和测试里还残留几类旧职责：

- intent classifier 仍有 `chat`、`inspect`、`design`、`implement` 等旧细分标签。
- 本地 `resolve_turn_intent()` 同时返回 intent 和 goal，和 "intent 是 intent，goal 是
  goal" 的边界冲突。
- title 仍有单独 title agent 调用，和首轮 structured resolver 重复。
- compaction 没有作为纯 runtime workflow node 接入 workflow context。
- 部分 graph/tool state 还保留旧 top-level 兼容字段。
- skill picker 在空 query 时全局 skill 会排在 project skill 前面，导致项目内选择不稳定。

本 spec 是 follow-up 决策，不修改既有 specs。

## Decisions

### 1. Intent 只分类 coding/general

`TaskIntent` 只保留：

```python
class TaskIntent(str, Enum):
    CODING = "coding"
    GENERAL = "general"
```

默认 intent 是 `coding`。只有明确非 workspace / 非代码任务才返回 `general`。
旧的 `chat`、`inspect`、`design`、`review`、`implement`、`debug`、`ambiguous`
不再属于 intent classifier 输出。

### 2. Local intent fallback 不产出 goal

本地 fallback 只判断 coarse intent：

```python
def resolve_turn_intent(...) -> GoalResolution:
    return GoalResolution(intent=TaskIntent.CODING, goal=None, ...)
```

本地逻辑可以保留 deterministic approval confirmation，但不能根据关键词生成新 goal。
Goal 只来自 structured goal resolver、明确 tool state patch，或显式 slash goal 操作。

### 3. Goal resolver 是 top-level turn 的第一步

每个 top-level user turn 开始时，voidx 主 agent 调用一次 structured LLM，返回
`GoalResolution`。当 structured call 失败或没有 model 时，fallback 只写 intent，不写 goal。

Pending approval 是 deterministic guard：如果存在 pending approval 且用户输入是明确确认语，
runtime 必须优先返回 confirmed approval，不能被 LLM 的 `general` 或空 goal 覆盖。

### 4. Title 并入首次 goal resolver

首次用户消息进入 goal resolver 时，如果 session 还没有稳定 title，resolver schema 允许
返回 `title: str | None`。

规则：

- `title` 可为空；消息太短或不适合生成标题时返回空。
- `title` 只在首轮、当前 title 仍是临时标题或默认标题时应用。
- 不再为 title 保留独立 LLM 调用或独立 workflow node。
- title 不进入 conversation transcript。

### 5. Compaction 是纯 runtime workflow

Compaction 不由用户委派，也不通过 title/goal resolver 合并。它是 runtime 主动触发的
LLM 工作流，形态类似 "runtime 构造 query -> LLM 处理 -> workflow context 约束输出"。

要求：

- 增加 `compaction` workflow node。
- 通过 `workflow_context_for(..., runtime_trigger="compaction")` 选择。
- 使用 hidden `compaction` persona prompt。
- context frame 记录 `frame_kind="compaction"` 和 `agent_persona="compaction"`。

### 6. Turn 结束时不能丢 resolver 写入的 goal

`turn_runner` 写入 `turn_task_state` 后，graph 返回时应：

```python
final_task_state = _load_task_state(final.get("task_state"), fallback=turn_task_state)
```

如果 graph 没返回 `task_state`，使用 resolver 之后的 `turn_task_state`。正常 graph path
返回 `task_state` 时仍以 graph 结果为准。

### 7. 不做旧字段兼容

删除旧 top-level runtime/task 字段兼容：

- `task_intent`
- `pending_approval`
- `workflow_runs`
- `current_goal`
- `intent_resolution_reason`
- `intent_confidence`
- `intent_source`
- `intent_refined`

这些字段不能继续作为 `AgentState` 顶层状态传播。工具需要修改任务状态时，只能返回
`ToolStatePatch` 的结构化字段，并由 runtime 写回 `TaskState`。

### 8. Skill picker project 优先

Skill autocomplete 在候选排序时 project skill 优先于 global skill。同名 skill 仍由
registry 的覆盖规则处理，但空 query 和宽 query 下项目本地 skill 应稳定排在全局 skill 前。

## Implementation Plan

1. 更新 `GoalResolution`：增加 optional `title`。
2. 修改 `resolve_turn_intent()`：只返回 `coding/general`，不生成 goal；保留 pending
   approval deterministic confirmation。
3. 修改 `resolve_goal_for_turn()`：接收 `title_requested`，prompt 中说明 title 规则；
   pending approval confirmation 优先于 structured model 输出。
4. 修改 `turn_runner`：首轮且当前 title 可替换时要求 resolver 生成 title；应用非空 title；
   final task state fallback 到 `turn_task_state`。
5. 删除独立 title agent 调用路径或使其不再调 LLM。
6. 增加 `compaction` workflow node，并在 compaction agent 构建消息时注入 runtime workflow
   context。
7. 删除 tool executor 对旧顶层字段的 fallback 和旧 refinement 字段传播。
8. 更新 intent classifier 训练脚本、测试数据和内置 artifact，使 labels 只有
   `coding/general`。
9. 更新 skill picker 排序和测试。

## Testing

- `infer_task_intent()` 和 classifier 只返回 `coding/general`。
- 无 model / resolver failure 时 `TaskState.current_goal` 不因本地 fallback 自动生成。
- pending approval confirmation 不被 LLM structured output 覆盖。
- 首轮 resolver 可以返回 title；空 title 不更新 session title。
- graph 返回缺少 `task_state` 时，resolver 写入的 `turn_task_state` 被保留。
- compaction context frame 包含 compaction persona 和 compaction workflow context。
- `ToolStatePatch` 不再传播旧 refinement 顶层字段。
- skill autocomplete project candidates 排在 global candidates 前。
