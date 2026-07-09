# Agent Tool Workflow 化调用契约 — 技术设计

> **Status: Done**

## Context

`AgentTool`（`src/voidx/tools/agent.py`）是 voidx 指派子 agent 的唯一入口。当前 tool 调用契约仍然偏向“指定一个 persona 跑一个子循环”：

- 调用方必须传 `persona`，但 runtime 里 workflow 已经能决定 persona。
- 调用方必须传 `max_steps`，但 step budget 属于 runtime 策略，不应暴露给 LLM 拼参数。
- 子 agent 自己从 `persona` 推断 `intent` / `goal`，这和主 agent 的 `resolve_goal_for_turn` 契约不一致。
- `expected_output` / `parent_evidence` 是自由文本，并通过关键词做隐式校验，LLM 不容易一次填对。

目标是把 `agent` tool 从“persona 委派工具”改成“workflow 子任务启动工具”：调用方显式给出一个和主 agent resolver 兼容的 `goal_resolution`，子 agent 必须进入 workflow，persona 随 workflow 自动决定，结果用结构化 `result` 契约返回。

## 当前问题

### 问题 1：LLM 需要填写 runtime 内部参数

当前 `AgentInput` 有 8 个字段：

| 字段 | 必填 | 问题 |
|------|------|------|
| `agent` | 否 | 仍保留 agent identity，默认 `voidx` |
| `persona` | 是 | 应由 workflow 决定，不应由调用方指定 |
| `description` | 是 | 合理，但需要保持自包含 |
| `model` | 否 | 可保留为高级覆盖 |
| `max_steps` | 是 | runtime 策略，不应由 LLM 指定 |
| `delegation_reason` | 是 | 委派策略不应暴露为子 agent 必填入参 |
| `expected_output` | 是 | 自由文本，和结构化结果契约重复 |
| `parent_evidence` | 是 | 自由文本，存在关键词隐式校验 |

`max_steps` 和 `persona` 是最明显的泄漏：前者是执行预算，后者已经可以从 workflow active node 推导。

### 问题 2：子 agent 没有显式任务状态

主 agent 每轮会得到 `GoalResolution(intent, goal, plan)`，并写入 `TaskState.current_intent`、`TaskState.current_goal` 和 `TaskState.workflow_route`。子 agent 当前没有接收这套结构，而是在 `run_subagent` 中通过 persona 推断：

- `review` -> `goal_type=review`
- `plan` -> `goal_type=design`
- `implement` -> `goal_type=feature/bugfix`
- `explore` -> `goal_type=inspect`

这种推断会让子 agent 的上下文比主 agent 弱，也会让 workflow route 无法成为强约束。

### 问题 3：workflow route 不是必填契约

当前 `plan.join` / `plan.leave` 只存在于主 resolver 结果中。子 agent 可以不进入 workflow，或者只靠 persona 激活 workflow。新的契约应要求：

- `goal_resolution.plan.join` 必须存在。
- `goal_resolution.plan.leave` 必须存在。
- `plan.join` 和 `plan.leave` 必须是已知 workflow node。
- 如果 LLM 没给，tool 返回简短提示，不启动子 agent。

### 问题 4：输出契约仍是自由文本

`expected_output` 要求子 agent 怎么写，但不是结构化字段。`review` 场景还依赖 `verdict` / `PASS` / `FAIL` / `NEEDS_CHANGE` 关键词匹配。新的调用契约应显式要求 `result`，并让子 agent 最终输出可解析的结构化结果。

## 目标

1. 子 agent 必须进入 workflow：调用方必须指定 `goal_resolution.plan.join` 和 `goal_resolution.plan.leave`。
2. 移除 `max_steps` 入参：执行预算由 runtime 内部策略决定。
3. 子 agent 接收和主 resolver 相同语义的 `intent` / `goal` / `plan`。
4. 移除 `persona` 入参：runtime 根据 workflow runs 决定 persona。
5. 子 agent 返回结构化 `result`。
6. 缺少关键字段时，tool 返回简短、可修复的提示。

## 非目标

- 不在本设计中重新设计 workflow DAG。
- 不在本设计中改变主 agent 的 `resolve_goal_for_turn` 输出语义。
- 不在本设计中让子 agent 和用户交互。
- 不要求一次性删除所有兼容逻辑；可以分阶段迁移测试和调用点。

## 推荐方案

### 新输入模型

将 `AgentInput` 改为面向 workflow 子任务：

```python
class AgentResultContract(BaseModel):
    schema_name: str = Field(
        default="agent_result",
        description="Name of the structured result contract the child agent must return.",
    )
    format: str = Field(
        description="Concrete structured result fields and allowed values.",
    )


class AgentInput(BaseModel):
    agent: str = Field(default="voidx", description="Child agent identity to run. Use voidx.")
    description: str = Field(
        description=(
            "Complete, self-contained task description for the child agent. "
            "Caller conversation history is not inherited."
        )
    )
    goal_resolution: GoalResolution = Field(
        description=(
            "Child task intent, required goal, and workflow route. "
            "plan.join and plan.leave are required and must name workflow nodes."
        )
    )
    result: AgentResultContract = Field(
        description="Structured result the child agent must return."
    )
    model: str | None = Field(default=None, description="Optional model override.")
```

字段变化：

| 旧字段 | 新处理 |
|--------|--------|
| `persona` | 删除，由 workflow active runs 推导 |
| `max_steps` | 删除，由 runtime 内部策略决定 |
| `delegation_reason` | 删除，委派策略由 orchestrator/prompt 决定 |
| `expected_output` | 删除，合并到 `result.format` |
| `parent_evidence` | 删除，放入 `description` 或 `goal_resolution.goal.desc` |
| `goal_resolution` | 新增，复用主 resolver 语义 |
| `result` | 新增，要求结构化输出契约 |

### 调用示例

Review 子任务：

```json
{
  "agent": "voidx",
  "description": "Review the AgentTool API migration plan. Focus on workflow routing, compatibility risks, and tests.",
  "goal_resolution": {
    "intent": {
      "type": "coding",
      "desc": "delegated workflow review"
    },
    "goal": {
      "type": "review",
      "desc": "review AgentTool workflow API migration"
    },
    "plan": {
      "join": "review",
      "leave": "review"
    }
  },
  "result": {
    "schema_name": "review_result",
    "format": "Return JSON-like fields: verdict=PASS|FAIL|NEEDS_CHANGE, findings, risks, verification_notes, next_actions."
  }
}
```

Implementation 子任务：

```json
{
  "agent": "voidx",
  "description": "Implement the AgentTool schema migration and update focused tests.",
  "goal_resolution": {
    "intent": {
      "type": "coding",
      "desc": "delegated implementation"
    },
    "goal": {
      "type": "feature",
      "desc": "migrate AgentTool to workflow-based child task API"
    },
    "plan": {
      "join": "tdd",
      "leave": "verify"
    }
  },
  "result": {
    "schema_name": "implementation_result",
    "format": "Return JSON-like fields: status, files_changed, tests_run, risks, followups."
  }
}
```

## Validation Rules

`AgentTool.execute` should reject before launching a child agent when:

- `description.strip()` is too short or not self-contained.
- `goal_resolution.intent.type` is missing.
- `goal_resolution.goal` is missing.
- `goal_resolution.plan` is missing.
- `goal_resolution.plan.join` is empty.
- `goal_resolution.plan.leave` is empty.
- `plan.join` or `plan.leave` is not a known workflow node.
- `result.format` is empty.
- `goal.type == "review"` but `plan.join != "review"`.

Error messages should be short and directly repairable. Examples:

- `Child agent delegation rejected. Provide goal_resolution.plan.join and plan.leave.`
- `Child agent delegation rejected. goal_resolution.goal is required.`
- `Child agent delegation rejected. plan.join must be a known workflow node.`
- `Child agent delegation rejected. result.format is required.`
- `Child agent delegation rejected. review goals must enter plan.join=review.`

## Runtime Flow

1. `AgentTool` validates `AgentInput`.
2. It converts `goal_resolution` into a child `TaskState`:
   - `current_intent = goal_resolution.intent.type`
   - `current_goal = goal_resolution.goal`
   - `workflow_route = WorkflowRoute(join=plan.join, leave=plan.leave)`
3. The child run reconciles workflow runs from `goal_resolution`.
4. Runtime derives `persona` from active workflow runs using existing workflow persona policy.
5. Runtime derives interaction mode from workflow/persona policy instead of accepting `persona` as input.
6. Runtime chooses step budget internally.
7. Child prompt includes the `result` contract and must finish with structured result fields.
8. `AgentTool` returns the child output plus metadata containing `intent`, `goal`, `workflow_route`, and `result.schema_name`; subagent lifecycle metadata records the derived persona, step budget, and finish details.

## Step Budget Policy

Remove `max_steps` from tool schema and choose the budget inside runtime. Initial policy can be simple:

| Workflow | Budget |
|----------|--------|
| `review` | 4 |
| `verify` | 4 |
| `plan` / `design-doc` | 5 |
| `debug` / `tdd` / `feedback` | 6 |
| fallback | 5 |

This keeps the model from inventing budgets while preserving bounded child loops. The exact numbers can move to a helper such as `_subagent_step_budget(goal_resolution)`.

## Structured Result Contract

`result` is a contract, not the final result at call time. The child agent must return final text shaped by this contract. The first implementation should enforce this through prompt/context instructions and metadata. A dedicated `finish_child_task` tool is not part of this design; the child agent's final output is the result.

Recommended minimum result formats:

| Goal type | `schema_name` | Required fields |
|-----------|---------------|-----------------|
| `review` | `review_result` | `verdict`, `findings`, `risks`, `verification_notes`, `next_actions` |
| `feature` / `bugfix` / `refactor` | `implementation_result` | `status`, `files_changed`, `tests_run`, `risks`, `followups` |
| `design` | `design_result` | `decision`, `options_considered`, `recommended_plan`, `open_questions` |
| `inspect` | `inspection_result` | `summary`, `evidence`, `risks`, `recommended_next_steps` |

## Migration Plan

### Phase 1: Schema and validation

- Add `AgentResultContract`.
- Replace `persona`, `max_steps`, `delegation_reason`, `expected_output`, and `parent_evidence` with `goal_resolution` and `result`.
- Add validation for required `plan.join` / `plan.leave`.
- Validate workflow node names against `DEFAULT_WORKFLOW_DAG.nodes`.
- Update rejection messages with short repair hints.
- Update schema tests to assert removed and added fields.

### Phase 2: Runtime wiring

- Change `AgentTool` runner call to pass `goal_resolution` and `result`.
- Change `Graph._subagent_runner` to accept `goal_resolution` instead of `runtime_persona` and `max_steps`.
- Build child `TaskState` from `goal_resolution`.
- Reconcile child workflow runs and derive persona from active workflow runs.
- Replace persona-based `_subagent_task_intent_for_agent` and `_subagent_goal_type_for_agent` usage for delegated runs.
- Select step budget internally.

### Phase 3: Result contract prompting

- Add result contract text to child runtime context.
- Ensure final child response follows `result.format`.
- Store `result.schema_name` in subagent metadata/events.
- Keep the external return value compatible as text initially, but shape it as structured text.

### Phase 4: Cleanup

- Remove obsolete review keyword validation. ✅ Done — removed with `expected_output` field in Phase 1.
- Remove runner compatibility checks for old `max_steps` / `persona` signatures once tests and callers are migrated. ✅ Done — runner already uses `goal_resolution` + `result_contract`.
- Update workflow docs and prompts to use `goal_resolution` examples. ✅ Done — prompts and tool description use new schema.
- Remove `delegation_reason` handling from the public agent tool schema and related tests. ✅ Done — field removed in Phase 1.
- Remove `WorkflowRoute._migrate_legacy_names` and migrate test code from `start`/`end` to `join`/`leave`. ✅ Done.

## Tests

Focused tests should cover:

- Tool schema no longer requires `persona`, `max_steps`, `delegation_reason`, `expected_output`, or `parent_evidence`.
- Tool schema requires `description`, `goal_resolution`, and `result`.
- Missing `goal_resolution.plan.join` rejects with a short repair hint.
- Missing `goal_resolution.plan.leave` rejects with a short repair hint.
- Missing `goal_resolution.goal` rejects with a short repair hint.
- Unknown workflow node rejects before child execution.
- Review goals reject unless `plan.join=review`.
- Child runner receives `goal_resolution` and `result`.
- Child runtime derives persona from workflow runs.
- Step budget is chosen internally and appears in metadata/events.
- Review child result prompt requires `verdict=PASS|FAIL|NEEDS_CHANGE`.

Relevant focused commands:

```bash
.venv/bin/python -m pytest tests/test_tools/test_basic.py -v
.venv/bin/python -m pytest tests/test_agent/test_core_flow.py -v
.venv/bin/python -m pytest tests/test_workflow_reconcile.py -v
```

## Decisions

- `delegation_reason` is removed from the public schema. Delegation policy belongs to the orchestrator and prompts, not this tool's required arguments.
- `goal_resolution.goal` is required. A child agent without a concrete goal should not be launched; `intent` may be `general`, but general child tasks should be rare.
- No `finish_child_task` tool is planned. The child agent's final output should follow the `result` contract directly.
