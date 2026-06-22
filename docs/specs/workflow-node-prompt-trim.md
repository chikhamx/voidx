# Workflow Node Prompt 精简方案 — 技术设计文档

## Context

Workflow node 定义在 `VOIDX_WORKFLOW_CONTEXT` 段中被渲染为 markdown，作为稳定 prompt 缓存注入每次 LLM 调用。当前 8 个 node 总计约 14,400 字符（~3,600 tokens），其中包含大量对 LLM 行为无实际约束力的描述性元数据。这些字段占用 prompt 空间但不影响输出质量，应予精简。

### 当前渲染结构（每个 node）

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
- 不改变 `render_node_summary`（当前未被调用，不在本次范围）

## 精简方案

### 移除的段落

| 段落 | 移除理由 | 受影响代码 |
|------|---------|-----------|
| `### Persona` | persona 已在 `Current Task State` 段实时呈现（`- Current persona: {name}`），workflow context 是稳定缓存，此处重复且可能过时 | `render.py:61-62` |
| `### Input` / `### Output` | IO 定义是描述性文字，runtime 从不按 schema 校验这些字段；信息与 Gate/Rules 高度重叠；LLM 不会返回结构化对象 | `render.py:63-68` |
| `### Tools` | 工具可用性由 permission engine 动态控制，静态列表可能过时；与 Gate 的 `denied_tools` 语义重叠 | `render.py:69-73` |
| `### Available Exits` | 边信息已在 DAG overview（`render_dag_overview`）中以 `source --condition--> target` 格式呈现，node 内重复列出 | `render.py:98-107` |

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

## 预估收益

当前 8 个 node 总计 ~14,400 字符（~3,600 tokens）。精简后预估：

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
| `src/voidx/workflow/context.py` | 调用 `render_node_markdown`，接口不变 |
| `src/voidx/workflow/policy.py` | `workflow_tools`、`workflow_personas` 仍供 runtime 使用 |
| `src/voidx/workflow/service.py` | `WorkflowMatch.body` 仍调用 `render_workflow_instruction` |
| `src/voidx/workflow/dag.py` | DAG 定义不变 |

## 实现步骤

1. **修改 `render_node_markdown`**：移除 Persona、Input/Output、Tools、Available Exits 四个段落的渲染代码
2. **运行现有测试**：确认哪些测试因渲染结构变化而失败
3. **更新测试断言**：按精简后的渲染结构更新所有失败测试
4. **全量测试**：`python -m pytest tests/ -v` 确认绿色

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 仅修改渲染层，保留 schema 字段 | 同时删除 schema 中的 io/persona/tools | `persona` 和 `tools` 被 runtime 代码（policy.py、reconcile.py、service.py）广泛使用；`io` 虽无代码引用但保留 schema 完整性以备将来 |
| 移除 Available Exits | 保留但压缩为单行 | DAG overview 已完整呈现边信息，node 内重复无增量价值 |
| 保留 Description | 合并 Description 到 Goal | Description 侧重"何时触发"，Goal 侧重"做什么"，语义不同但可共存；压缩为一句话更紧凑但损失触发条件信息 |

## Open Questions

- [ ] Description 是否应压缩为更短的触发条件一句话？（当前保留原文，可后续优化）
