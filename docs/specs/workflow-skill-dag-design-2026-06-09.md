# Workflow Skill DAG Runtime 编排设计

> **Status: In Progress**
> **Date:** 2026-06-09
> **Scope:** 将 8 个内置 workflow skill 组成全局 DAG，由 runtime 层驱动编排

## 问题

当前 workflow skill 的执行依赖 LLM 遵从性：

1. **Gate 无强制力**：brainstorming 说"设计批准前不能写代码"，但 LLM 可以忽略，runtime 不会拦截 write/edit
2. **条件边无 runtime 支持**：brainstorming 的 Decision Rules（小改动→TDD、用户说 just implement→writing-plans）只存在于 skill body 文本中，`WORKFLOW_SKILL_TRANSITIONS` 只实现了无条件出边
3. **闭环无保障**：verification 失败应回退到 TDD 或 debugging，但 runtime 不会自动回退，依赖 LLM 自行判断
4. **Skill body 与 policy 不同步**：skill body 声明的 transition 和 policy 中的 `WORKFLOW_SKILL_TRANSITIONS` 容易出现不一致（如 `create` 死代码、systematic-debugging 缺 TDD 出边）

## 目标

将 8 个内置 skill 组成一张全局 DAG，runtime 层根据"当前在哪个 skill 节点"来：

1. **约束 LLM 行为**：当前 skill 的 gate 决定 LLM 能用什么工具、必须做什么
2. **驱动条件转移**：skill 完成后，根据条件自动激活下一个 skill
3. **保障闭环**：验证失败自动回退，review 返回问题自动进入修复循环

## 全局 DAG

### 节点与条件边

```
                        ┌─────────────────────────────────┐
                        │         brainstorming            │
                        │  Gate: 禁止 write/edit           │
                        └──────┬──────────┬───────────────┘
                               │          │
              正常流程(设计批准) │          │ 条件: 小改动
                               │          │
                               ▼          ▼
                    writing-design-docs   test-driven-development
                    Gate: 必须过 reader   Gate: 必须先写测试
                    test 才算完成          才能写实现
                               │                    │
                               ▼                    │
                        writing-plans               │
                        Gate: plan 必须             │
                        可执行                      │
                               │                    │
              条件: 用户说      │                    │
              "just implement" │                    │
              ─────────────────┤                    │
                               │                    │
                               ▼                    ▼
                        test-driven-development ◄───┘
                               │
                               ▼
                verification-before-completion
                Gate: 必须运行验证命令
                才能声称完成
                    │           │
        验证通过    │           │ 验证失败
                    ▼           ▼
        requesting-     test-driven-development
        code-review     (实现问题回退)
        或               或
                        systematic-debugging
                        (bug 回退)
                    │
                    ▼
            requesting-code-review
            Gate: 不能不经 review
            就标记完成
                    │
            条件: review 返回
            FAIL/NEEDS_CHANGE
                    │
                    ▼
            receiving-code-review
            Gate: 不能未验证反馈
            就改代码
                    │
                    ▼
            test-driven-development + verification-before-completion
            (循环)
```

Debug 入口：

```
systematic-debugging
Gate: 必须找到根因才能修
        │
        │ 条件: 非平凡修复
        ├──────────────────► test-driven-development
        │
        │ 条件: 平凡修复
        └──────────────────► verification-before-completion
```

### 条件边完整表

| 源节点 | 条件 | 目标节点 | 当前状态 |
|--------|------|---------|---------|
| brainstorming | 正常流程（设计被批准） | writing-design-docs | ✅ policy 已实现 |
| brainstorming | 用户说 "just implement it" / 已有详细 spec | writing-plans | ❌ 无 runtime 支持 |
| brainstorming | 小改动（重命名、加配置、修 typo） | test-driven-development | ❌ 无 runtime 支持 |
| writing-design-docs | 完成 | writing-plans | ✅ policy 已实现 |
| writing-plans | plan 被批准 | test-driven-development | ✅ policy 已实现 |
| test-driven-development | 实现完成 | verification-before-completion | ✅ policy 已实现 |
| verification-before-completion | 验证通过 + substantial work | requesting-code-review | ✅ policy 已实现 |
| verification-before-completion | 验证失败（实现问题） | test-driven-development | ❌ 无 runtime 支持 |
| verification-before-completion | 验证失败（bug） | systematic-debugging | ❌ 无 runtime 支持 |
| requesting-code-review | review 返回 FAIL/NEEDS_CHANGE | receiving-code-review | ❌ 无 runtime 支持 |
| receiving-code-review | 反馈验证通过 | test-driven-development + verification | ✅ policy 已实现 |
| systematic-debugging | 非平凡修复 | test-driven-development | ❌ 无 runtime 支持 |
| systematic-debugging | 平凡修复 | verification-before-completion | ✅ policy 已实现 |

## 设计

### 1. DAG 声明式定义

将 `WORKFLOW_SKILL_TRANSITIONS` 从简单的无条件出边升级为带条件的出边：

```python
# src/voidx/skills/policy.py

from dataclasses import dataclass


@dataclass(frozen=True)
class ConditionalEdge:
    """A directed edge in the workflow skill DAG."""
    target: str
    condition: str  # machine-readable condition key
    label: str = ""  # human-readable description


WORKFLOW_SKILL_DAG: dict[str, list[ConditionalEdge]] = {
    "brainstorming": [
        ConditionalEdge("writing-design-docs", "approved", "design approved"),
        ConditionalEdge("writing-plans", "skip_to_plan", "user says 'just implement it' or spec is detailed"),
        ConditionalEdge("test-driven-development", "small_change", "small scoped change"),
    ],
    "writing-design-docs": [
        ConditionalEdge("writing-plans", "completed", "doc passes reader test"),
    ],
    "writing-plans": [
        ConditionalEdge("test-driven-development", "approved", "plan approved"),
    ],
    "test-driven-development": [
        ConditionalEdge("verification-before-completion", "implemented", "implementation complete"),
    ],
    "verification-before-completion": [
        ConditionalEdge("requesting-code-review", "passed_substantial", "verification passed after substantial work"),
        ConditionalEdge("test-driven-development", "failed_implementation", "verification failed — implementation issue"),
        ConditionalEdge("systematic-debugging", "failed_bug", "verification failed — bug found"),
    ],
    "requesting-code-review": [
        ConditionalEdge("receiving-code-review", "review_has_issues", "review returned FAIL or NEEDS_CHANGE"),
    ],
    "receiving-code-review": [
        ConditionalEdge("test-driven-development", "feedback_valid", "feedback verified and valid"),
        ConditionalEdge("verification-before-completion", "feedback_valid", "feedback implemented, needs verification"),
    ],
    "systematic-debugging": [
        ConditionalEdge("test-driven-development", "nontrivial_fix", "fix requires TDD"),
        ConditionalEdge("verification-before-completion", "trivial_fix", "fix is trivial"),
    ],
}
```

**关键变化**：
- 每条出边有 `condition` 字段，标识在什么条件下走这条边
- 一个 skill 可以有多条出边，运行时根据条件选择
- 条件是 machine-readable 的 key，不是自然语言

### 2. Skill 节点的 Gate 约束

每个 skill 节点声明它对 LLM 行为的约束：

```python
# src/voidx/skills/policy.py

@dataclass(frozen=True)
class SkillGate:
    """Runtime-enforced constraints for a skill node."""
    denied_tools: tuple[str, ...] = ()  # tools that cannot be used while this skill is active
    required_before_transition: str = ""  # what must be true before this skill can transition


WORKFLOW_SKILL_GATES: dict[str, SkillGate] = {
    "brainstorming": SkillGate(
        denied_tools=("write", "edit", "apply_patch", "lsp_format"),
        required_before_transition="design approved by user",
    ),
    "writing-design-docs": SkillGate(
        denied_tools=(),
        required_before_transition="doc passes reader test",
    ),
    "writing-plans": SkillGate(
        denied_tools=("write", "edit", "apply_patch", "lsp_format"),
        required_before_transition="plan is executable with exact paths and commands",
    ),
    "test-driven-development": SkillGate(
        denied_tools=(),
        required_before_transition="test written and verified red before implementation",
    ),
    "verification-before-completion": SkillGate(
        denied_tools=(),
        required_before_transition="verification command run with evidence",
    ),
    "requesting-code-review": SkillGate(
        denied_tools=(),
        required_before_transition="review requested with required brief fields",
    ),
    "receiving-code-review": SkillGate(
        denied_tools=(),
        required_before_transition="feedback verified against codebase before implementing",
    ),
    "systematic-debugging": SkillGate(
        denied_tools=("write", "edit", "apply_patch", "lsp_format"),
        required_before_transition="root cause identified with evidence",
    ),
}
```

**Gate 执行点**：在 `_authorize_tool_calls()` 中检查当前 active skill 的 `denied_tools`，拒绝被 gate 禁止的工具调用。

### 3. 条件判定

条件边的 `condition` 由谁判定？两种机制：

#### 3a. LLM 判定（通过 skill_decision 工具）

新增一个 barrier 工具 `skill_decision`，LLM 在 skill 完成时必须调用它来声明结果：

```python
class SkillDecisionInput(BaseModel):
    skill: str = Field(description="Name of the skill being decided")
    condition: str = Field(description="Which condition edge to take (must match a ConditionalEdge.condition)")
    evidence: str = Field(description="Brief evidence supporting this decision")
    summary: str = Field(description="What was accomplished in this skill")


class SkillDecisionTool(BaseTool):
    id = "skill_decision"
    description = (
        "Declare the outcome of the current workflow skill and choose the next step. "
        "You MUST call this when a workflow skill is complete before proceeding to the next phase. "
        "The condition must match one of the declared edges for this skill in the workflow DAG."
    )
```

**执行逻辑**：

1. LLM 调用 `skill_decision(skill="brainstorming", condition="approved", evidence="user said 'looks good'", summary="design for X approved")`
2. Runtime 验证 `condition` 是否是当前 skill 的合法出边
3. 如果合法，将当前 skill 标记为 SATISFIED，激活目标 skill
4. 如果不合法，返回错误，LLM 必须重新选择

**为什么用工具而不是从 AI message 中解析**：工具调用是结构化的，runtime 可以验证和强制。AI message 的自由文本无法可靠解析。

#### 3b. Runtime 自动判定（部分条件）

某些条件可以由 runtime 自动判定，不需要 LLM 声明：

| 条件 | 自动判定方式 |
|------|------------|
| `review_has_issues` | review agent 返回 verdict=FAIL 或 NEEDS_CHANGE |
| `failed_implementation` | verification 中 bash 返回非零 exit code |
| `failed_bug` | verification 中原始 bug 症状仍可复现 |
| `small_change` | intent 分类 + 用户文本关键词匹配 |
| `skip_to_plan` | 用户文本包含 "just implement it" 等关键词 |

这些条件在 `_state_update_from_executed_tools()` 中自动检测，不需要 LLM 显式调用 `skill_decision`。

### 4. 与现有架构的集成

#### 4a. Gate 在权限层执行

```python
# src/voidx/agent/graph/tool_execution.py — _authorize_tool_calls()

async def _authorize_tool_calls(self, tool_calls, *, agent_name, ...):
    # ... existing logic ...

    # 新增：检查 active skill gate
    active_skills = _active_skill_names(state.get("skill_runs", []) or [])
    denied = set()
    for skill_name in active_skills:
        gate = WORKFLOW_SKILL_GATES.get(skill_name)
        if gate:
            denied.update(gate.denied_tools)

    approved = [tc for tc in tool_calls if tc["name"] not in denied]
    denied_by_gate = [tc for tc in tool_calls if tc["name"] in denied]

    if denied_by_gate:
        # 返回拒绝消息，说明哪个 skill gate 阻止了操作
        for tc in denied_by_gate:
            gate_msg = f"Blocked by {skill_name} gate: {gate.required_before_transition}"
            denied_list.append((tc, gate_msg))
```

#### 4b. 条件边在 skill state 推进中执行

```python
# src/voidx/skills/runtime.py — advance_skill_states()

# 当 skill 被 SATISFIED 时，不再无条件激活所有 transition_to
# 而是根据条件选择激活哪些目标
def _activate_transition_targets(states, run, *, turn_count, condition=None):
    edges = WORKFLOW_SKILL_DAG.get(run.name, [])
    for edge in edges:
        if condition and edge.condition != condition:
            continue  # 条件不匹配，跳过这条边
        # ... 激活目标 skill ...
```

#### 4c. skill_decision 工具注册

```python
# src/voidx/tools/registry.py — 注册新工具
# skill_decision 是 barrier tool，优先执行
```

#### 4d. Task Context 注入当前 skill 约束

```python
# src/voidx/agent/runtime_context.py — _current_task_state()

# 在 Current Task State 中注入 gate 约束
for skill_name in active_skill_names:
    gate = WORKFLOW_SKILL_GATES.get(skill_name)
    if gate and gate.denied_tools:
        lines.append(f"- Skill gate [{skill_name}]: denied tools = {', '.join(gate.denied_tools)}")
    if gate and gate.required_before_transition:
        lines.append(f"- Skill gate [{skill_name}]: must satisfy '{gate.required_before_transition}' before proceeding")
```

### 5. 消息流示例

#### 场景：用户说"实现一个新功能"

```
Turn 1: 用户 "给 auth 模块加一个 rate limiter"
  │
  ├─ resolve_turn_intent() → intent=implement
  ├─ workflow_skill_activations() → brainstorming (design/create intent)
  │    注：implement intent 当前直接激活 TDD，但按 DAG 应先走 brainstorming
  │    需要调整：新功能 → brainstorming，明确修改 → TDD
  │
  ├─ Gate: brainstorming active → denied_tools = [write, edit, apply_patch]
  ├─ LLM 只能 read/grep/glob/clarify → 探索代码、提问
  ├─ LLM 提出设计方案，用户批准
  ├─ LLM 调用 skill_decision(skill="brainstorming", condition="approved", ...)
  ├─ Runtime: brainstorming → SATISFIED, 激活 writing-design-docs
  │
  └─ Turn 结束，skill state 持久化

Turn 2: 用户 "继续"
  │
  ├─ skill_runs: brainstorming=satisfied, writing-design-docs=active
  ├─ Gate: writing-design-docs active → 无工具限制，但必须过 reader test
  ├─ LLM 写设计文档，自检 reader test
  ├─ LLM 调用 skill_decision(skill="writing-design-docs", condition="completed", ...)
  ├─ Runtime: writing-design-docs → SATISFIED, 激活 writing-plans
  │
  └─ Turn 结束

Turn 3: 用户 "继续"
  │
  ├─ skill_runs: writing-plans=active
  ├─ Gate: writing-plans active → denied_tools = [write, edit, apply_patch]
  ├─ LLM 写实施计划
  ├─ LLM 调用 skill_decision(skill="writing-plans", condition="approved", ...)
  ├─ Runtime: writing-plans → SATISFIED, 激活 test-driven-development
  │
  └─ Turn 结束

Turn 4+: TDD → verification → review 循环
```

### 6. 向后兼容

- `WORKFLOW_SKILL_TRANSITIONS` 保留为 DAG 的简化视图（无条件边的默认出边），供不需要条件判断的场景使用
- `skill_decision` 工具是可选的——如果 LLM 不调用它，skill 不会自动 SATISFIED，gate 约束持续生效，LLM 最终会被迫调用
- 现有的 `advance_skill_states()` 和 `SkillRunState` 保持不变，`transition_to` 字段从 DAG 动态计算

### 7. 需要调整的现有逻辑

| 现有逻辑 | 调整 |
|---------|------|
| `workflow_skill_activations()` 中 intent=implement 直接激活 TDD | 新功能请求应先走 brainstorming，只有明确的小修改才直接走 TDD |
| `WORKFLOW_SKILL_TRANSITIONS` 只有无条件出边 | 升级为 `WORKFLOW_SKILL_DAG` 带条件边 |
| `_authorize_tool_calls()` 不检查 skill gate | 增加 gate denied_tools 检查 |
| `_state_update_from_executed_tools()` 不处理自动条件判定 | 增加 review verdict / bash exit code 等自动条件检测 |
| `SkillRunState.transition_to` 是静态列表 | 改为从 DAG 动态计算，包含条件信息 |

### 8. 风险

| 风险 | 缓解 |
|------|------|
| Gate 过严导致 LLM 无法完成合理操作 | brainstorming/writing-plans 的 gate 只禁止写文件，不禁止 read/bash(readonly)/clarify |
| 条件判定错误导致走错分支 | LLM 可以通过 skill_decision 显式选择，runtime 验证合法性 |
| skill_decision 增加额外 tool call 开销 | 只在 skill 完成时调用一次，每 turn 最多 1 次 |
| DAG 定义与 skill body 文本不一致 | DAG 是 single source of truth，skill body 的 transition 描述改为引用 DAG |
| 多个 active skill 的 gate 冲突 | 取并集：任一 skill 禁止的工具都被禁止 |

## 修改清单

| # | 文件 | 修改内容 | 优先级 |
|---|------|---------|--------|
| 1 | `src/voidx/skills/policy.py` | `WORKFLOW_SKILL_TRANSITIONS` → `WORKFLOW_SKILL_DAG` + `WORKFLOW_SKILL_GATES` | P0 |
| 2 | `src/voidx/tools/skill_decision.py` | 新增 `skill_decision` barrier 工具 | P0 |
| 3 | `src/voidx/agent/graph/tool_execution.py` | `_authorize_tool_calls()` 增加 gate denied_tools 检查 | P0 |
| 4 | `src/voidx/agent/runtime_context.py` | Current Task State 注入 gate 约束信息 | P1 |
| 5 | `src/voidx/skills/runtime.py` | `advance_skill_states()` 支持条件边选择 | P1 |
| 6 | `src/voidx/skills/policy.py` | `workflow_skill_activations()` 调整 implement intent 的激活逻辑 | P1 |
| 7 | `src/voidx/agent/graph/tool_execution.py` | `_state_update_from_executed_tools()` 增加自动条件检测 | P2 |
| 8 | `src/voidx/agent/agents.py` | orchestrator tools 列表增加 `skill_decision` | P1 |
| 9 | `src/voidx/agent/agents.py` | BASE_SYSTEM_PROMPT 更新 Workflow Skills section | P1 |
