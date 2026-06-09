# Workflow Skill 逻辑自洽性与任务闭环审查

> **Status: In Progress**
> **Date:** 2026-06-09
> **Scope:** 提示词层、Skill 层、运行时激活层的逻辑一致性

## 背景

voidx 的 workflow skill 系统由三层构成：

1. **提示词层** (`agents.py`) — 定义 agent 角色和行为规则
2. **Skill 层** (8 个 bundled SKILL.md) — 定义工作流技能的 gate / workflow / transition
3. **运行时层** (`policy.py`, `runtime.py`, `context.py`, `instruction.py`) — 负责激活、注入、推进 skill 状态

三层需要保持一致：skill body 声明的行为必须与 policy 的激活逻辑和 transition 图对齐，否则 LLM 收到矛盾信号。

## 发现的问题

### 问题 1：brainstorming Decision Rules 快捷路径无运行时支持

**位置**：`src/voidx/skills/bundled/brainstorming/SKILL.md` Decision Rules

**现状**：brainstorming 的 Decision Rules 声明了三条快捷路径：

- 用户说 "just implement it" → skip to writing-plans
- 用户请求已是详细 spec → go directly to writing-plans
- 小范围变更（重命名、加配置字段、修 typo）→ go directly to test-driven-development

但 `workflow_skill_activations()` 中，当 intent=design/create 时只激活 brainstorming，不会根据用户文本语义跳到 writing-plans 或 test-driven-development。这些快捷路径完全依赖 LLM 自行从 skill body 读取并遵循，运行时不会激活目标 skill。

**影响**：LLM 可能忽略 Decision Rules，在应该跳过 brainstorming 时仍走完整设计流程，或在跳转后 Current Task State 中没有对应 skill 为 active，导致 LLM 缺少明确的执行指导。

**建议**：在 `workflow_skill_activations()` 中增加快捷路径的激活逻辑。具体方案：

```python
# brainstorming Decision Rules 快捷路径
if intent == "design" or intent == "create":
    add("brainstorming", "design/create intent")
    # 快捷路径：用户明确说"just implement it"或请求已是详细 spec
    if _contains_any(text, _SKIP_TO_PLAN_TERMS):
        add("writing-plans", "brainstorming shortcut: skip to plan")
    # 快捷路径：小范围变更直接走 TDD
    elif _contains_any(text, _SMALL_CHANGE_TERMS):
        add("test-driven-development", "brainstorming shortcut: small change → TDD")
    else:
        if _contains_any(text, _PLAN_TERMS):
            add("writing-plans", "planning intent")
```

新增关键词列表：

```python
_SKIP_TO_PLAN_TERMS = (
    "just implement it",
    "直接实现",
    "直接做",
    "不用设计",
    "skip design",
    "go ahead and implement",
)

_SMALL_CHANGE_TERMS = (
    "rename",
    "add a config",
    "fix typo",
    "small change",
    "minor change",
    "重命名",
    "加个配置",
    "小改动",
    "小修改",
)
```

---

### 问题 2：systematic-debugging → TDD 的 transition 缺失

**位置**：`src/voidx/skills/policy.py` WORKFLOW_SKILL_TRANSITIONS + workflow_skill_activations

**现状**：

- Skill body Phase 3 说 "For non-trivial fixes, follow test-driven-development"
- `WORKFLOW_SKILL_TRANSITIONS["systematic-debugging"] = ("verification-before-completion",)` — 只 transition 到 verification，没有到 TDD
- 当 intent=debug 时，`workflow_skill_activations()` 同时激活 systematic-debugging 和 verification-before-completion，但不激活 TDD

**影响**：debug 场景下需要写非平凡修复时，LLM 缺少 TDD skill 的明确指导，Current Task State 中也没有 TDD 为 active。

**建议**：在 debug intent 下同时激活 TDD：

```python
if intent == "debug":
    add("systematic-debugging", "debug intent")
    add("test-driven-development", "debug may require TDD for non-trivial fixes")
    add("verification-before-completion", "debug lifecycle")
```

同时在 transition 图中补充：

```python
WORKFLOW_SKILL_TRANSITIONS["systematic-debugging"] = (
    "test-driven-development",
    "verification-before-completion",
)
```

---

### 问题 3：`create` intent 在 policy 中是死代码

**位置**：`src/voidx/skills/policy.py` line 74

**现状**：

```python
if intent == "design" or intent == "create":
    add("brainstorming", "design/create intent")
```

但 `TaskIntent` 枚举只有 `chat, inspect, design, review, implement, debug, ambiguous`，没有 `create`。`intent == "create"` 永远不会为真。

**影响**：包含 "create" 语义的用户请求（如 "create a new feature"）会被分类为 implement 或 design，不会触发 "design/create intent" 这条激活路径。功能上没有 bug（因为 design intent 已经覆盖），但代码有误导性。

**建议**：移除 `create` 条件，只保留 `design`：

```python
if intent == "design":
    add("brainstorming", "design intent")
```

或者，如果确实想支持 create 作为独立 intent，在 `TaskIntent` 枚举中新增 `CREATE`，并在 `infer_task_intent()` 中添加对应的关键词匹配。

---

### 问题 4：writing-design-docs 引用不存在的模板路径

**位置**：`src/voidx/skills/bundled/writing-design-docs/SKILL.md` Workflow Step 3

**现状**：Skill body 说 "Load the template — read the template file at `templates/{doc_type}.md` (workspace root)"。但 `doc_template.py` 工具已被删除，workspace root 下也没有 `templates/` 目录。

**影响**：LLM 尝试读取不存在的模板文件，浪费一个 tool call，然后需要自行推断文档结构。

**建议**：两种方案选一：

**方案 A**：创建 `templates/` 目录并补充模板文件。在 workspace root 下创建：

- `templates/tech-design.md`
- `templates/prd.md`
- `templates/rfc.md`
- `templates/api-doc.md`
- `templates/readme.md`

**方案 B**（更轻量）：修改 skill body，移除对模板文件的引用，改为在 skill body 中内联文档结构骨架：

```markdown
3. **Use the document structure** — follow the structure below for the 
   identified document type. Replace `{placeholder}` fields with actual 
   content. If information is insufficient, mark it `[TBD]`.

### tech-design structure:
- Context & Motivation
- Goals & Non-Goals
- Architecture Overview
- Detailed Design (with code examples)
- Alternatives Considered
- Migration / Rollout Plan
- Open Questions

### prd structure:
- Problem Statement
- User Stories / Requirements
- Success Metrics
- Interaction Design (states, edge cases, error handling)
- Data Spec
- Out of Scope
```

推荐方案 B，因为模板文件需要随项目维护，而内联结构随 skill body 版本管理更可靠。

---

## LLM 遵从性优化

### 问题：LLM 可能不严格遵守 "只看 active skill"

**现状**：所有 bundled skill body 始终全量注入为 "reference library"，通过 Current Task State 中的 "Active workflow skills" 标识哪些是当前激活的。Skill context header note 说：

> "Follow only skills listed as active in Current Task State or the user explicitly references that skill."

**问题**：LLM 可能忽略这个指示，将非 active skill 的 gate/workflow/transition 也当作当前指令执行。特别是当非 active skill 的内容与当前任务表面相关时（如 debug 场景下看到 TDD skill body），LLM 可能混淆。

**为什么不能改为按需注入**：全量注入是为了让 skill context message 跨 turn 内容稳定，命中 provider 的 prompt cache（prefix 级别）。如果每轮只注入 active skill 的 body，内容随 intent 变化，缓存就废了。当前的消息布局：

```
[SystemMessage: stable sections]              ← 命中 prefix cache
[HumanMessage: skill context (全量)]          ← 命中 cache（跨 turn 不变）
[HumanMessage: task context (每轮变)]         ← 短，cache miss 影响小
[对话历史...]
```

### 建议 1：加强 skill context header note 的措辞

**当前**（`src/voidx/skills/context.py`）：

```python
_SKILL_CONTEXT_REFERENCE_LIBRARY_NOTE = (
    "These bundled skill bodies are a reference library. Follow a skill body "
    "only when Current Task State lists that skill under Active workflow skills "
    "or the user explicitly references that skill. Do not treat inactive skill "
    "bodies as active instructions."
)
```

**建议改为**：

```python
_SKILL_CONTEXT_REFERENCE_LIBRARY_NOTE = (
    "These bundled skill bodies are a reference library. You MUST ONLY follow "
    "skills explicitly listed under 'Active workflow skills' in Current Task State, "
    "or skills the user explicitly references by name. Treating an inactive skill "
    "body as active instructions is a critical error — do not follow its gate, "
    "workflow, or transition instructions."
)
```

变更要点：
- "Follow a skill body only when" → "You MUST ONLY follow" — 更强的指令语气
- "Do not treat inactive skill bodies as active instructions" → "Treating an inactive skill body as active instructions is a critical error — do not follow its gate, workflow, or transition instructions" — 明确列出不能做什么

### 建议 2：在 Current Task State 中显式列出 inactive skills

**当前**（`src/voidx/agent/runtime_context.py` `_current_task_state()`）：

```python
if self.active_skill_summaries:
    lines.append(f"- Active workflow skills: {'; '.join(self.active_skill_summaries)}")
```

**建议增加**：

```python
if self.active_skill_summaries:
    lines.append(f"- Active workflow skills: {'; '.join(self.active_skill_summaries)}")
    # 显式标注 inactive skills，防止 LLM 误用
    active_names = {s.split(' ')[0].strip() for s in self.active_skill_summaries}
    all_bundled = {"brainstorming", "writing-design-docs", "writing-plans",
                   "test-driven-development", "verification-before-completion",
                   "requesting-code-review", "receiving-code-review", "systematic-debugging"}
    inactive = all_bundled - active_names
    if inactive:
        lines.append(f"- Inactive reference skills: {', '.join(sorted(inactive))} (do NOT follow)")
```

变更要点：
- 显式列出 inactive skills 并标注 "do NOT follow"，消除歧义
- LLM 不需要自己推断哪些是 inactive

### 建议 3：在 BASE_SYSTEM_PROMPT 的 Workflow Skills section 中补充约束

**当前**（`src/voidx/agent/agents.py`）：

```python
## Workflow Skills

- voidx has a workflow skill system.
- Current Task State is the activation source for this turn's workflow skills.
- Skill Context messages contain bundled workflow skill bodies as a reference
  library. Follow only skills listed as active in Current Task State, unless the
  user explicitly references another skill.
- load_skills can return project/global skill bodies for the current turn.
```

**建议改为**：

```python
## Workflow Skills

- voidx has a workflow skill system.
- Current Task State is the activation source for this turn's workflow skills.
- Skill Context messages contain bundled workflow skill bodies as a reference
  library. Follow ONLY skills listed as active in Current Task State, unless the
  user explicitly references another skill by name.
- When a skill is NOT listed as active, its body is reference only — do NOT
  follow its gate, workflow, or transition instructions.
- load_skills can return project/global skill bodies for the current turn.
```

变更要点：
- "Follow only" → "Follow ONLY" — 大写强调
- "unless the user explicitly references another skill" → "unless the user explicitly references another skill by name" — 更精确
- 新增一条显式约束，明确 inactive skill 的 body 不能执行

## 修改清单

| # | 文件 | 修改内容 | 优先级 |
|---|------|---------|--------|
| 1 | `src/voidx/skills/policy.py` | 移除 `create` 死代码条件 | P2 |
| 2 | `src/voidx/skills/policy.py` | brainstorming 快捷路径激活逻辑 | P1 |
| 3 | `src/voidx/skills/policy.py` | debug intent 增加 TDD 激活 | P1 |
| 4 | `src/voidx/skills/policy.py` | systematic-debugging transition 补充 TDD | P1 |
| 5 | `src/voidx/skills/bundled/writing-design-docs/SKILL.md` | 移除模板路径引用，内联文档结构 | P2 |
| 6 | `src/voidx/skills/context.py` | 加强 header note 措辞 | P1 |
| 7 | `src/voidx/agent/runtime_context.py` | Current Task State 显式列出 inactive skills | P1 |
| 8 | `src/voidx/agent/agents.py` | BASE_SYSTEM_PROMPT 补充 inactive skill 约束 | P1 |

## 风险

- **修改 policy.py 激活逻辑**：可能改变现有 intent 下的 skill 激活行为，需要回归测试
- **Current Task State 增加 inactive 列表**：增加少量 token 开销（~50 tokens/turn），但换来更明确的 LLM 指导
- **writing-design-docs 内联结构**：skill body 变长，但消除了对外部模板文件的依赖
