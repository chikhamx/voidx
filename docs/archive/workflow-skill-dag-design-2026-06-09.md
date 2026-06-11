# Workflow DAG Runtime 编排设计

> **Status: Done**
> **Date:** 2026-06-09
> **Scope:** 将 8 个内置 workflow node 组成全局 DAG，由 runtime 层驱动编排
> **Source of truth:** `workflow-skill-struct-design-2026-06-09.md`

> **Implementation note:** 最终运行时代码位于 `src/voidx/workflow/`：`nodes.py` 定义内置节点，`dag.py` 定义 `DEFAULT_WORKFLOW_DAG`，`policy.py`/`runtime.py`/`service.py` 驱动激活、gate 与转移。内置 workflow 不再通过 `SKILL.md` 注入。

## 定位

本文定义 workflow node 的运行时编排目标：active node、gate、条件边、验证失败回退和 review 循环。

结构化定义不在本文重复维护。`WorkflowNode`、`Edge`、`IntentEntry`、`WorkflowDAG`、node 列表和 DAG 边表以 `workflow-skill-struct-design-2026-06-09.md` 为准。本文只描述 runtime 应如何消费这些结构。

## 问题

迁移前 workflow skill 的执行依赖 LLM 遵从性：

1. **Gate 无强制力**：brainstorming 说"设计批准前不能写代码"，但 runtime 不会拦截 write/edit/apply_patch
2. **条件边无 runtime 支持**：brainstorming 的 Decision Rules 只存在于迁移前 Markdown body 文本中，旧手写 transition 表只实现了无条件出边
3. **闭环无保障**：verification 失败应回退到 TDD 或 debugging，review 返回问题应进入 receiving-code-review，但 runtime 不会自动推进
4. **Markdown body 与 policy 不同步**：迁移前 Markdown body、policy transition、prompt context 可能描述三套不同流程

## 目标

Runtime 层根据当前 active workflow node：

1. **约束工具调用**：active node 的 gate 决定哪些工具暂时不可用
2. **暴露当前出口**：Current Task State 告诉 LLM 当前 node 的 gate 和可选 outgoing edges
3. **驱动条件转移**：node 完成后，通过结构化出口 condition 激活下一个 node
4. **保障闭环**：验证失败和 review 失败进入明确回退分支
5. **保持单一事实源**：DAG、gate、priority、Workflow Context 渲染都从 `WorkflowDAG` 和 `WorkflowNode` 推导

## 全局 DAG

### 节点与条件边

```
                        ┌─────────────────────────────────┐
                        │         brainstorming            │
                        │  Gate: 禁止 write/edit           │
                        └──────┬──────────┬───────────────┘
                               │          │
              approved         │          │ small_change
                               │          │
                               ▼          ▼
                    writing-design-docs   test-driven-development
                    Gate: reader test     Gate: red before green
                               │                    │
                               ▼                    │
                        writing-plans               │
                        Gate: plan approved         │
                               │                    │
                               ▼                    ▼
                        test-driven-development ◄───┘
                               │
                               ▼
                verification-before-completion
                Gate: evidence before completion
                    │           │
        passed      │           │ failed_implementation / failed_bug
                    ▼           ▼
        requesting-code-review  test-driven-development
                    │           或 systematic-debugging
                    ▼
            receiving-code-review
                    │
                    ▼
            test-driven-development + verification-before-completion
```

Debug 入口：

```
systematic-debugging
Gate: 必须找到根因才能修
        │
        ├── nontrivial_fix ──► test-driven-development
        └── trivial_fix ─────► verification-before-completion
```

### 条件边完整表

| 源节点 | 条件 | 目标节点 | 当前状态 |
|--------|------|---------|---------|
| brainstorming | `approved` | writing-design-docs | ✅ DAG edge + `advance_workflow` 支持 |
| brainstorming | `skip_to_plan` | writing-plans | ✅ DAG edge + `advance_workflow` 支持 |
| brainstorming | `small_change` | test-driven-development | ✅ DAG edge + `advance_workflow` 支持 |
| writing-design-docs | `completed` | writing-plans | ✅ DAG edge + `advance_workflow` 支持 |
| writing-plans | `approved` | test-driven-development | ✅ DAG edge + `advance_workflow` 支持 |
| test-driven-development | `implemented` | verification-before-completion | ✅ DAG edge + `advance_workflow` 支持 |
| verification-before-completion | `passed_substantial` | requesting-code-review | ✅ DAG edge + `advance_workflow` 支持 |
| verification-before-completion | `failed_implementation` | test-driven-development | ✅ DAG edge + `advance_workflow` 支持；自动判定未实现 |
| verification-before-completion | `failed_bug` | systematic-debugging | ✅ DAG edge + `advance_workflow` 支持；自动判定未实现 |
| requesting-code-review | `review_has_issues` | receiving-code-review | ✅ DAG edge + `advance_workflow` 支持；自动判定未实现 |
| receiving-code-review | `feedback_valid` | test-driven-development | ✅ DAG edge + `advance_workflow` 支持 |
| receiving-code-review | `feedback_verified` | verification-before-completion | ✅ DAG edge + `advance_workflow` 支持 |
| systematic-debugging | `nontrivial_fix` | test-driven-development | ✅ DAG edge + `advance_workflow` 支持 |
| systematic-debugging | `trivial_fix` | verification-before-completion | ✅ DAG edge + `advance_workflow` 支持 |

## 运行时设计

### 1. DAG 声明式定义

DAG 不应再以 `policy.py` 中的手写 transition / gate 表作为事实源。

目标结构：

- `WorkflowNode` 自包含：name、description、triggers、priority、gate、workflow、decision_rules
- `WorkflowDAG` 编排：nodes、edges、intent_map
- `policy.py` 从 `DEFAULT_WORKFLOW_DAG` 推导 priority、transition、entry nodes、gate
- Workflow Context 从 `WorkflowNode` + outgoing edges 渲染，避免 markdown body 与 runtime policy 分叉

具体 schema 和 8 个 node 定义见 `workflow-skill-struct-design-2026-06-09.md`。

### 2. Gate 在权限层执行

Gate 来自 active nodes 的 `NodeGate.denied_tools`。权限层在授权 tool calls 时取并集：

```python
active_nodes = active_workflow_node_names(state.skill_runs)
denied_tools = DEFAULT_WORKFLOW_DAG.all_denied_tools(active_nodes)

if tool_call.name in denied_tools:
    deny(tool_call, reason=f"Blocked by active workflow gate: {gate.description}")
```

执行原则：

- 任一 active node 禁止的工具都被禁止
- Gate 只拦截明确声明的工具，不做自然语言推断
- Gate 拒绝应返回可恢复错误，提示当前 active node 和 required condition
- read-only 探索工具不应被 brainstorming / writing-plans / systematic-debugging 禁止

### 3. 出口选择工具：`advance_workflow`

统一使用 `advance_workflow`，不要再引入 `skill_decision`。

`advance_workflow` 负责在当前 node 完成后选择一条 outgoing edge：

```python
class AdvanceWorkflowInput(BaseModel):
    condition: str = Field(
        description=(
            "The transition condition to take from the current workflow node. "
            "Must match one of the outgoing edge conditions defined in the workflow DAG. "
            "Use 'done' to end the current node without transitioning."
        )
    )
    evidence: str = Field(description="Brief evidence that the condition is satisfied")
    summary: str = Field(description="What was accomplished in the current workflow node")
```

执行逻辑：

1. 找到当前 active workflow node
2. 验证 `condition` 是否是该 node 的合法 outgoing edge
3. 若合法：当前 node 标记为 satisfied，激活 edge.target
4. 若 `condition == "done"`：当前 node 标记为 satisfied，不激活后继
5. 若不合法：返回可用 condition 列表，要求 LLM 重试

为什么使用工具：

- condition 是结构化字段，runtime 可验证
- evidence / summary 可进入 `SkillEvidence`
- 避免解析 AI message 自由文本

### 4. Runtime 自动判定（未实现）

早期设计提出部分 condition 可由 runtime 自动产生事件，不必要求 LLM 显式调用 `advance_workflow`：

| 条件 | 自动判定方式 |
|------|------------|
| `review_has_issues` | review agent 返回 verdict=FAIL 或 NEEDS_CHANGE |
| `failed_implementation` | verification 命令非零，且失败来自刚改动的实现路径 |
| `failed_bug` | 原始 bug 症状仍复现，或验证输出显示新 bug |
| `small_change` | intent + 用户文本 + diff scope 满足小修改规则 |
| `skip_to_plan` | 用户明确要求跳过设计，或输入已经是详细 spec |

最终实现尚未落地自动判定。当前所有条件仍需 LLM 显式调用 `advance_workflow`，runtime 只负责校验 condition 是否属于当前 active node 的 outgoing edge，并据此推进状态。

### 5. Current Task State 注入

每轮 task context 应包含：

- active workflow nodes
- 每个 active node 的 gate 摘要
- 每个 active node 的可用 outgoing edges
- 已满足 / blocked / skipped 的 workflow run state

示例：

```text
- Active workflow nodes: brainstorming (design intent)
- Workflow gate [brainstorming]: denied tools = write, edit, apply_patch, lsp_format
- Workflow gate [brainstorming]: must satisfy design approved by user before proceeding
- Workflow exits [brainstorming]: approved -> writing-design-docs; skip_to_plan -> writing-plans; small_change -> test-driven-development; done -> end
```

Current Task State 只列 active node 的运行态信息；inactive node 只在 `VOIDX_WORKFLOW_CONTEXT` 中以摘要形式提供发现和转移背景。

### 6. Backward Compatibility

- `voidx.skills.policy.workflow_skill_transitions(name)` 作为兼容 alias 保留，但从 `DEFAULT_WORKFLOW_DAG.edges_from(name)` 推导
- `WorkflowRunState.transition_to` 保存目标 node 名称；`SkillRunState` 仅作为兼容 alias 保留
- 内置 workflow node 通过 `VOIDX_WORKFLOW_CONTEXT` 注入给 LLM，不再作为 `SkillDefinition.body`
- `advance_workflow` 已加入 orchestrator 工具白名单，并由 intent refinement 暴露给需要推进 workflow 的回合
- global/project skill 继续使用 SKILL.md，不参与 runtime gate / DAG 编排
- 若 LLM 不调用 `advance_workflow`，node 不自动进入后继；gate 持续生效，或在 terminal node 上结束

## 需要调整的现有逻辑

| 现有逻辑 | 调整 |
|---------|------|
| 旧手写 intent→skill 分支 | `workflow_activations()` 从 `DEFAULT_WORKFLOW_DAG` 和 policy 规则推导 |
| 旧手写 transition 表只有无条件出边 | `WORKFLOW_TRANSITIONS` 从 `WorkflowDAG.edges` 推导 transition view |
| `_authorize_tool_calls()` 不检查 workflow gate | 增加 active node gate denied_tools 检查 |
| `_state_update_from_executed_tools()` 不处理自动条件判定 | 未实现；保留为后续项 |
| `SkillRunState.transition_to` 是静态列表 | `WorkflowRunState.transition_to` 改为 DAG 派生的兼容摘要 |
| SKILL.md 手写 gate / transition | Workflow Context 从结构化 node + DAG 渲染 |

## 风险

| 风险 | 缓解 |
|------|------|
| Gate 过严导致 LLM 无法完成合理操作 | gate 只禁止写类工具，保留 read/bash(readonly)/clarify |
| 条件判定错误导致走错分支 | 自动判定保守；LLM 可通过 `advance_workflow` 显式选择 |
| `advance_workflow` 增加额外 tool call | 只在 node 完成时调用；terminal node 可 `done` |
| DAG 与 Workflow Context 文本不一致 | Struct 是 single source of truth，context body 自动渲染 |
| 多个 active node gate 冲突 | denied tools 取并集；出口选择只处理当前 active node |

## 修改清单

| # | 文件 | 修改内容 | 优先级 |
|---|------|---------|--------|
| 1 | `src/voidx/workflow/schema.py` | 新增 WorkflowNode / WorkflowDAG schema，见 Struct 设计 | P0 |
| 2 | `src/voidx/workflow/nodes.py` | 8 个内置 workflow node 结构化定义 | P0 |
| 3 | `src/voidx/workflow/dag.py` | 新增 `DEFAULT_WORKFLOW_DAG` | P0 |
| 4 | `src/voidx/tools/advance_workflow.py` | 新增出口选择工具 | P0 |
| 5 | `src/voidx/agent/graph/permissions.py` | `_authorize_tool_calls()` 增加 gate denied_tools 检查 | P0 |
| 6 | `src/voidx/workflow/runtime.py` | `advance_workflow_states()` 支持 condition + evidence | P1 |
| 7 | `src/voidx/agent/runtime_context.py` | Current Task State 注入 gate 和 outgoing edges | P1 |
| 8 | `src/voidx/workflow/policy.py` | priority / transition / activation 从 DAG 推导 | P1 |
| 9 | `src/voidx/workflow/render.py` | 从 WorkflowNode + DAG 渲染 Workflow Context body | P1 |
| 10 | `src/voidx/workflow/service.py` | workflow node selection、runs、context | P1 |
