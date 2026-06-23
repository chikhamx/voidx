# Workflow Skill 逻辑自洽性与任务闭环审查

> **Status: Done**
> **Date:** 2026-06-09
> **Scope:** 当前 SKILL.md + policy.py 实现的短期一致性修补
> **Long-term source of truth:** `workflow-skill-struct-design-2026-06-09.md`
> **Runtime target:** `workflow-skill-dag-design-2026-06-09.md`

> **Implementation note:** 本审查中的短期 `SKILL.md` 对齐路线已被最终架构替代：内置 workflow 现在由 `src/voidx/workflow/` 的结构化节点和 DAG 表达，`SKILL.md` 只属于外部 Markdown skill 系统。

## 定位

本文是过渡期审查，不是最终架构设计。

长期方案应以 `WorkflowNode` + `WorkflowDAG` 为事实源，并由 `advance_workflow` 负责 node 出口选择。本文只保留当前实现中值得先修、且不会与结构化 DAG 迁移冲突的问题。

## 当前实现状态

当前内置 workflow 系统由三层构成：

1. **提示词层**：`src/voidx/agent/agents.py`
2. **结构化定义层**：`src/voidx/workflow/nodes.py`、`schema.py`、`dag.py`
3. **运行时层**：`src/voidx/workflow/policy.py`、`runtime.py`、`context.py`、`service.py`、`src/voidx/agent/runtime_context.py`

内置 workflow 不再通过 `SKILL.md` 或 SkillRegistry 注入。Project/global Markdown skills 仍由 `src/voidx/skills/` 处理；`voidx.skills.runtime/policy` 仅为兼容转发层。

## 保留的短期修补项

### 问题 1：`create` intent 在 policy 中是死代码

**位置**：`src/voidx/workflow/policy.py`

当前代码：

```python
if intent == "design" or intent == "create":
    add("brainstorming", "design/create intent")
```

但 `TaskIntent` 只有 `chat, inspect, design, review, implement, debug, ambiguous`，没有 `create`。这不会直接造成功能缺失，但会误导后续维护者。

**短期建议**：

```python
if intent == "design":
    add("brainstorming", "design intent")
```

同时更新测试中对 `"design/create intent"` 的断言。

**长期归宿**：由 `WorkflowDAG.intent_map` 替代手写 intent 分支。

### 问题 2：systematic-debugging 到 TDD 的路径缺失

**位置**：`src/voidx/workflow/policy.py`

`systematic-debugging` node 写明：非平凡修复应 follow `test-driven-development`。最终实现已在 debug intent 中激活 TDD，并在 DAG 中保留 `nontrivial_fix -> test-driven-development`、`trivial_fix -> verification-before-completion` 两条条件边。

**短期建议**：

```python
if intent == "debug":
    add("systematic-debugging", "debug intent")
    add("test-driven-development", "debug may require TDD for non-trivial fixes")
    add("verification-before-completion", "debug lifecycle")
```

**风险**：debug 场景会多激活一个 workflow node，提示上下文更强但 token 略增。

**长期归宿**：Struct/DAG 中通过两条条件边表达：

- `nontrivial_fix -> test-driven-development`
- `trivial_fix -> verification-before-completion`

### 问题 3：inactive workflow node 的约束措辞偏弱

**位置**：

- `src/voidx/workflow/context.py`
- `src/voidx/agent/agents.py`

当前 Workflow Context 对 active node 注入完整定义，对 inactive node 只注入摘要；active node 仍由 Current Task State 标识。这个设计需要明确告诉 LLM 不要执行 inactive node 的 gate/workflow/transition。

**最终实现**：

在 workflow context header 中使用 runtime-owned workflow 口径：

```python
_WORKFLOW_CONTEXT_NOTE = (
    "These are structured workflow definitions owned by the voidx runtime. "
    "Active workflow nodes are expanded with full instructions. Inactive nodes "
    "are summarized for discovery and transition context only."
)
```

在 `BASE_SYSTEM_PROMPT` 的 Workflow Runtime 段补同类约束：

```markdown
- Workflow Context messages contain structured workflow node definitions as a
  reference library. Active node definitions are expanded; inactive nodes may
  appear only as summaries. Follow ONLY nodes listed as active in Current Task
  State, unless the user explicitly references another node by name.
- When a node is not listed as active, its summary is reference only. Do not
  follow its gate, workflow, or transition instructions.
```

**不建议短期做**：在 Current Task State 中硬编码列出所有 inactive nodes。这个列表容易和 `src/voidx/workflow/nodes.py` 漂移。

**长期归宿**：Current Task State 注入 active node 的 gate 和 outgoing edges，而不是列 inactive skills。

## 不再作为问题处理的项

### writing-design-docs 模板路径

之前认为 `templates/{doc_type}.md` 不存在。当前仓库根目录已经有：

- `templates/api-doc.md`
- `templates/prd.md`
- `templates/readme.md`
- `templates/rfc.md`
- `templates/tech-design.md`

因此不应移除 `writing-design-docs` 对模板路径的引用，也不需要把模板结构内联进 workflow node body。

长期结构化方案可以继续保留模板文件，也可以让 `WorkflowNode` 渲染时引用模板路径；这不阻塞 DAG/runtime 设计。

### brainstorming shortcut 的关键词补丁

原建议是在 workflow activation policy 中通过关键词直接激活 `writing-plans` 或 `test-driven-development`。

这个补丁不建议作为短期项直接实现，原因：

- `just implement it`、`直接实现`、`fix typo` 等文本大概率已被 intent 分类为 `implement`
- 在 `design` 分支额外做关键词判断，可能把正常设计请求误导到 implementation 流程
- 这些 shortcut 本质是条件边，应由 `advance_workflow` 或保守的 runtime 自动判定处理

短期如果必须处理，只应先补分类测试，确认真实落点，再针对具体误分类加窄规则。

## 与 Struct/DAG 主线的关系

| 短期修补 | 长期替代 |
|---------|---------|
| 删除 `create` 死代码 | `WorkflowDAG.intent_map` |
| debug 同时激活 TDD | `systematic-debugging` 条件边 |
| prompt/header 强化 inactive node 约束 | Current Task State 注入 active gate + outgoing edges |
| 手写 transition 补边 | 从 `WorkflowDAG.edges` 推导 |

## 修改清单

| # | 文件 | 修改内容 | 优先级 |
|---|------|---------|--------|
| 1 | `src/voidx/workflow/policy.py` | 移除 `create` 死代码条件 | P1 |
| 2 | `tests/test_skills.py` | 更新 `"design/create intent"` 相关断言 | P1 |
| 3 | `src/voidx/workflow/policy.py` | debug intent 增加 TDD 激活 | P1 |
| 4 | `src/voidx/workflow/policy.py` | systematic-debugging transition 补充 TDD | P1 |
| 5 | `src/voidx/workflow/context.py` | 加强 Workflow Context reference header | P1 |
| 6 | `src/voidx/agent/agents.py` | BASE_SYSTEM_PROMPT 补充 inactive node 约束 | P1 |

## 验证建议

短期修补后至少运行：

```bash
.venv/bin/python -m pytest tests/test_skills.py tests/test_agent/test_runtime_context.py -v
```

如果改动 Current Task State 渲染，再补跑：

```bash
.venv/bin/python -m pytest tests/test_agent/test_core_flow.py -v
```
