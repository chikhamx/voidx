> **Status: Done** — 变更已在工作目录中实施，所有测试通过。

# Workflow Node Prompt 精简方案 — 技术设计文档

## Context

Workflow node 定义在 `VOIDX_WORKFLOW_CONTEXT` 段中被渲染为 markdown，作为稳定 prompt 缓存注入每次 LLM 调用。实施前 8 个 node 总计约 14,400 字符（~3,600 tokens），其中包含大量对 LLM 行为无实际约束力的描述性元数据。这些字段占用 prompt 空间但不影响输出质量，已予精简。

### 精简前渲染结构（每个 node）

```
## Workflow Node: {name}
Description: ...
### Goal
### Persona          ← 冗余
### Input            ← 冗余
### Output           ← 冗余
### Tools            ← 冗余
### Gate
### Workflow
### Internal Subworkflow (仅 tdd/debug)
### Available Exits  ← 冗余
### Rules
### Exceptions (仅 tdd)
```

## Goals and Non-Goals

### Goals

- 从 workflow context 渲染中移除对 LLM 行为无约束力的冗余段落，节省 ~40% prompt tokens
- 保持 schema 字段不变（`persona`、`io`、`tools` 仍供 runtime 代码使用），仅调整渲染逻辑
- 确保所有现有测试通过或按新渲染结构更新

### Non-Goals

- 不修改 `WorkflowNode` schema（`io`、`persona`、`tools` 字段保留，runtime 代码仍在使用）
- 不修改 DAG edge 定义或 node 语义
- 不改变 `render_node_summary`（当前无调用方，不在本次范围）。注意：`render_node_summary` 中仍有 `dag.edges_from` 调用渲染 Exits 信息，与精简后的 `render_node_markdown` 不一致；若未来启用该函数，需同步精简

## 精简方案

### 移除的段落

| 段落 | 移除理由 | 受影响代码 |
|------|---------|-----------|
| `### Persona` | persona 已在 `Current Task State` 段实时呈现（`runtime_context.py:253` 的 `- Current persona: {name}`），workflow context 是稳定缓存，此处重复且可能过时 | `render_node_markdown` 中 `if node.persona` 分支 |
| `### Input` / `### Output` | IO 定义是描述性文字，runtime 从不按 schema 校验这些字段；信息与 Gate/Rules 高度重叠；LLM 不会返回结构化对象 | `render_node_markdown` 中 `if node.io.input/output` 分支 |
| `### Tools` | 工具可用性由 permission engine 动态控制（`workflow_tools()` + `workflow_denied_tools()`），静态列表可能过时；关键约束已由 Gate 的 `denied_tools` 覆盖 | `render_node_markdown` 中 `### Tools` 段落 |
| `### Available Exits` | 边信息已在 DAG overview（`render_dag_overview`）中以 `source --condition--> target` 格式呈现；active node 的退出路径还在 Current Task State 中实时渲染（`runtime_context.py:267-269`），node 内重复列出 | `render_node_markdown` 中 `if dag` + `edges` 分支 |

### 保留的段落

| 段落 | 保留理由 |
|------|---------|
| `## Workflow Node: {name}` | 标识 |
| `Description` | 触发条件，一句话 |
| `### Goal` | 核心语义，精炼 |
| `### Gate` | 行为护栏，`required_before_transition` 是关键约束 |
| `### Workflow` | 流程步骤指导 |
| `### Internal Subworkflow` | 循环语义和退出条件（tdd/debug） |
| `### Rules` | 精炼的行为规则 |
| `### Exceptions` | 逃生舱口（tdd） |

### 精简后渲染结构

```
## Workflow Node: {name}
Description: ...
### Goal
### Gate
### Workflow
### Internal Subworkflow (仅 tdd/debug)
### Rules
### Exceptions (仅 tdd)
```

## 对 LLM 行为的影响分析

移除的四个段落均为"稳定缓存中的静态信息"，而 LLM 实际决策依赖的是"每次 turn 动态渲染的 Current Task State"。静态信息与动态信息重叠时，LLM 以动态信息为准。移除静态冗余不会导致行为退化，反而减少了 prompt 中的信息冲突风险（如 persona 在两处不一致时 LLM 该信哪个）。

| 移除段落 | 对 LLM 行为的影响 | 替代信号来源 |
|---------|-------------------|------------|
| `### Persona` | 无 | Current Task State 中 `- Current persona: {name}` 每次 turn 实时渲染，比静态文本更准确 |
| `### Input` / `### Output` | 无 | runtime 不校验 LLM 输出的结构化字段；关键语义（如 exit 条件、路由规则）已被 Gate/Rules 覆盖 |
| `### Tools` | 极小 | runtime 通过 permission engine 强制控制工具可用性，LLM 看不到的工具不会被绑定；工具 schema 在 system prompt 中自描述 |
| `### Available Exits` | 极小 | active node 的退出路径由 Current Task State 中 `Workflow exits [{name}]` 实时渲染；非 active node 的退出路径对当前决策无意义 |

**唯一值得关注的风险**：如果未来某个 node 的 Input/Output 中引入了 runtime 会解析的结构化字段（当前没有），那时需要重新评估。但当前架构下，LLM 通过 `workflow` 工具的 condition 参数传递退出意图，不通过结构化输出，所以这个风险是理论性的。

## 预估收益

实施前 8 个 node 总计 ~14,400 字符（~3,600 tokens）。精简后预估（实施前测量值）：

| Node | 当前 chars | 精简后 chars | 节省 |
|------|-----------|-------------|------|
| brainstorm | 1,907 | ~1,100 | ~42% |
| design | 1,737 | ~950 | ~45% |
| plan | 1,769 | ~1,000 | ~43% |
| tdd | 1,648 | ~1,100 | ~33% |
| verify | 1,516 | ~850 | ~44% |
| review | 1,445 | ~750 | ~48% |
| feedback | 2,435 | ~1,400 | ~43% |
| debug | 1,915 | ~1,200 | ~37% |
| **总计** | **~14,400** | **~8,350** | **~42%** |

## 受影响的文件

### 必须修改

| 文件 | 变更 |
|------|------|
| `src/voidx/workflow/render.py` | `render_node_markdown` 中移除 Persona、Input/Output、Tools、Available Exits 的渲染逻辑 |
| `tests/test_agent/test_prompts.py` | 更新断言：不再检查 `### Persona`、`### Input`、`### Tools` 等段落 |
| `tests/test_agent/test_prepare_workflow.py` | 更新断言：检查精简后的渲染结构 |
| `tests/test_skills/test_skill_references.py` | 更新断言：`## Workflow Node: {name}` 仍存在，但不再检查被移除的段落 |
| `tests/test_skills/test_workflow_advance.py` | 更新断言：`### Internal Subworkflow` 仍存在 |
| `tests/test_agent/test_subagent_step_budget_final.py` | 更新断言 |
| `tests/test_agent/test_subagent_step_budget_convergence.py` | 更新断言 |

### 不需修改

| 文件 | 理由 |
|------|------|
| `src/voidx/workflow/schema.py` | schema 字段保留，仅渲染层精简 |
| `src/voidx/workflow/nodes.py` | node 定义不变 |
| `src/voidx/workflow/context.py` | 调用 `render_node_markdown`，接口不变；传入 `dag` 参数导致 Available Exits 当前被渲染，移除后该参数不再触发 Exits 渲染。`dag` 参数在 `render_node_markdown` 中已成为死参数，可后续清理 |
| `src/voidx/workflow/policy.py` | `workflow_tools`、`workflow_personas` 仍供 runtime 使用 |
| `src/voidx/workflow/service.py` | `WorkflowMatch.body` 仍调用 `render_workflow_instruction` |
| `src/voidx/workflow/dag.py` | DAG 定义不变 |
| `tests/test_agent/test_runtime_context_builder.py` | 断言 `## Workflow Node: not in system`，精简后仍成立，无需修改 |

## 实现步骤

1. **修改 `render_node_markdown`**：移除 Persona、Input/Output、Tools、Available Exits 四个段落的渲染代码
2. **更新测试断言**：按精简后的渲染结构更新所有受影响的测试文件（见"必须修改"清单）
3. **全量测试**：`python -m pytest tests/ -v` 确认绿色

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 仅修改渲染层，保留 schema 字段 | 同时删除 schema 中的 io/persona/tools | `persona` 和 `tools` 被 runtime 代码（policy.py、reconcile.py、service.py）广泛使用；`io` 虽无代码引用但保留 schema 完整性以备将来 |
| 移除 Available Exits | 保留但压缩为单行 | DAG overview 已完整呈现边信息，node 内重复无增量价值 |
| 保留 Description | 合并 Description 到 Goal | Description 侧重"何时触发"，Goal 侧重"做什么"，语义不同但可共存；压缩为一句话更紧凑但损失触发条件信息 |

## Open Questions

- [x] Description 是否应压缩为更短的触发条件一句话？→ 本次不做，保留原文。Description 与 Goal 语义不同，可后续单独优化