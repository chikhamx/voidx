# Workflow Node 结构化定义设计

> **Status: In Progress**
> **Date:** 2026-06-09
> **Scope:** 将 workflow skill 从自由格式 markdown 升级为结构化 WorkflowNode schema，支撑 DAG runtime 编排
> **Depends on:** `workflow-skill-dag-design-2026-06-09.md`

## 问题

当前 skill 定义是自由格式的 SKILL.md 文件：

1. **Runtime 无法解析 skill 语义**：gate、workflow、transition、decision rules 都写在 markdown body 里，只有 LLM 能读懂，runtime 无法提取结构化信息
2. **DAG 编排无法实现**：条件边、gate 约束、decision rules 都需要 machine-readable 的结构，自由文本无法支撑
3. **Skill 之间结构不统一**：8 个 skill 的 section 命名和内容组织各不相同（有的有 Gate，有的没有；有的有 Transition，有的没有）
4. **Skill body 与 policy 容易不同步**：transition 声明在 markdown 里，实际行为在 `policy.py` 里，两处维护

## 目标

1. Workflow skill 抽象为 **WorkflowNode**——独立的、自包含的节点，只声明 gate + workflow + decision_rules，不知道出边
2. Node 之间的边由 **Policy 层编排**（WorkflowDAG），Node 正交组合，可复用
3. SKILL.md 保留为 **LLM 可读的指令**，但从 WorkflowNode 自动生成，不再手写
4. `policy.py` 中的 DAG、gate、priority 全部从 WorkflowNode + WorkflowDAG 推导，single source of truth
5. 向后兼容：project/global skill 仍可用 SKILL.md，结构化只对 bundled workflow node 强制

## 核心设计原则：Node 与 Edge 分离

```
WorkflowNode（自包含，不知道出边）       WorkflowDAG（编排层，定义边）
┌──────────────────────┐           ┌──────────────────────────┐
│ name                 │           │ nodes: [WorkflowNode...] │
│ gate                 │           │ edges: [Edge...]         │
│ workflow             │    ←───   │   source → target        │
│ decision_rules       │           │   condition              │
│ anti_patterns        │           │ entry_nodes: [...]       │
│ allowed_exceptions   │           │ intent_map: {            │
│ extra_sections       │           │   debug → systematic-*,  │
└──────────────────────┘           │   design → brainstorming │
                                   │ }                        │
                                   └──────────────────────────┘
```

**为什么 Node 和 Edge 分离**：

1. **正交组合**：同一个 Node 可出现在不同 DAG 中，或同一 DAG 换不同边组合。`verification-before-completion` 在 debug 流程和 implement 流程中都用，但入边不同——Node 不需要知道谁连它
2. **扩展容易**：新增一个 Node 只需定义 gate + workflow，然后在 DAG 里加边，不需要改已有 Node
3. **Policy 可替换**：不同 interaction mode 可以用不同的 DAG 编排，Node 不变
4. **测试独立**：Node 的 gate/workflow 测试与 DAG 的边测试正交

## 当前结构 vs 目标结构

### 当前：SKILL.md 自由格式

```markdown
---
name: brainstorming
description: Use before creating features...
triggers:
  - create feature
  - design
---

# Brainstorming for voidx

Core rule: present a design and get user approval before writing any code.

## Gate
Do not write code, invoke implementation skills, or take implementation action
until the design is presented and approved.

## Workflow
1. Explore context
2. Ask clarifying questions
3. Propose 2-3 approaches
4. Present design for approval
5. Write design doc
6. Transition to writing-design-docs

## Decision Rules
- If the user says "just implement it" → skip to writing-plans
- Small changes → skip to test-driven-development
```

Runtime 只能解析 frontmatter（name, description, triggers），body 是不透明字符串。

### 目标：结构化 schema + 自动生成 body

```python
# src/voidx/skills/bundled/brainstorming.py

from voidx.skills.schema import (
    WorkflowNode,
    NodeGate,
    DecisionRule,
    WorkflowStep,
)

BRAINSTORMING = WorkflowNode(
    name="brainstorming",
    description="Use before creating features, building components, or modifying behavior.",
    triggers=[
        "create feature", "build component", "add functionality", "new feature",
        "design", "brainstorm", "refactor", "restructure",
        "新功能", "实现新功能", "设计", "头脑风暴", "需求澄清", "重构", "重组",
    ],
    priority=5,
    gate=NodeGate(
        denied_tools=("write", "edit", "apply_patch", "lsp_format"),
        description="Do not write code or take implementation action until the design is presented and approved.",
    ),
    core_rule="Present a design and get user approval before writing any code.",
    workflow=[
        WorkflowStep(order=1, action="Explore context", description="Check files, docs, recent commits to understand the current state."),
        WorkflowStep(order=2, action="Ask clarifying questions", description="One at a time. Understand purpose, constraints, and success criteria."),
        WorkflowStep(order=3, action="Propose 2-3 approaches", description="With trade-offs and your recommendation."),
        WorkflowStep(order=4, action="Present design for approval", description="Scaled to complexity. Get user approval."),
        WorkflowStep(order=5, action="Write design doc", description="Save to docs/specs/<topic>-design-YYYY-MM-DD.md."),
    ],
    # edges defined in WorkflowDAG, not in WorkflowNode
    decision_rules=[
        DecisionRule(condition="skip_to_plan", description="If the user explicitly says 'just implement it', skip to writing-plans but still confirm the goal in one sentence first."),
        DecisionRule(condition="skip_to_plan", description="If the user's request is already a detailed spec with clear requirements, confirm understanding and go directly to writing-plans."),
        DecisionRule(condition="small_change", description="For small, well-scoped changes (renaming, adding a config field, fixing a typo), confirm the goal in one sentence and go directly to test-driven-development."),
    ],
    anti_patterns=[
        '"This is too simple to need a design" is where unexamined assumptions cause the most wasted work.',
    ],
)
```

Runtime 可以直接读取 `gate.denied_tools`、`decision_rules`，不需要解析 markdown。边信息从 `WorkflowDAG` 获取。

## Schema 定义

### WorkflowNode — 自包含的节点

```python
# src/voidx/skills/schema.py — 新增

from pydantic import BaseModel, Field


class NodeGate(BaseModel):
    """Runtime-enforced constraints for a skill node."""
    denied_tools: tuple[str, ...] = ()
    description: str = ""  # LLM-readable explanation of the gate


class DecisionRule(BaseModel):
    """A rule that determines which edge condition to take.

    condition must match an Edge.condition in the WorkflowDAG,
    but the Node does not know which DAG it belongs to.
    """
    condition: str  # machine-readable condition key
    description: str  # LLM-readable explanation


class WorkflowStep(BaseModel):
    """An ordered step in the skill's workflow."""
    order: int
    action: str  # short action name
    description: str = ""  # LLM-readable detail


class WorkflowNode(BaseModel):
    """A self-contained workflow skill node.

    A Node declares its own gate, workflow, and decision rules.
    It does NOT declare edges — edges are defined in WorkflowDAG.
    This separation allows Nodes to be composed orthogonally
    in different DAGs.

    The SKILL.md body is auto-generated from this node.
    """
    name: str
    description: str = ""
    triggers: list[str] = Field(default_factory=list)
    priority: int = 999
    enabled: bool = True

    # Core rule — the one-sentence summary that appears at the top
    core_rule: str = ""

    # Gate — runtime-enforced constraints
    gate: NodeGate | None = None

    # Ordered workflow steps
    workflow: list[WorkflowStep] = Field(default_factory=list)

    # Decision rules that determine which condition to take
    # (conditions map to edges in WorkflowDAG)
    decision_rules: list[DecisionRule] = Field(default_factory=list)

    # Anti-patterns — things not to do
    anti_patterns: list[str] = Field(default_factory=list)

    # Allowed exceptions to the gate
    allowed_exceptions: list[str] = Field(default_factory=list)

    # Additional sections (key = section title, value = content)
    extra_sections: dict[str, str] = Field(default_factory=dict)
```

### Edge — DAG 中的有向边

```python
class Edge(BaseModel):
    """A directed edge in the workflow DAG.

    Defined at the policy/orchestration layer, not inside WorkflowNode.
    """
    source: str  # source node name
    target: str  # target node name
    condition: str  # machine-readable condition key
    label: str = ""  # human-readable description
```

### WorkflowDAG — 编排层

```python
class IntentEntry(BaseModel):
    """Maps a task intent to entry nodes in the DAG."""
    intent: str
    nodes: list[str]  # node names to activate
    reason: str = ""


class WorkflowDAG(BaseModel):
    """The orchestration layer that composes WorkflowNodes with edges.

    Nodes are self-contained; edges and entry points are defined here.
    Different DAGs can reuse the same nodes with different edge topologies.
    """
    name: str
    nodes: dict[str, WorkflowNode] = Field(default_factory=dict)
    edges: list[Edge] = Field(default_factory=list)
    intent_map: list[IntentEntry] = Field(default_factory=list)

    def edges_from(self, node_name: str) -> list[Edge]:
        """Get all outgoing edges from a node."""
        return [e for e in self.edges if e.source == node_name]

    def edges_to(self, node_name: str) -> list[Edge]:
        """Get all incoming edges to a node."""
        return [e for e in self.edges if e.target == node_name]

    def entry_nodes(self, intent: str) -> list[WorkflowNode]:
        """Get entry nodes for a given intent."""
        for entry in self.intent_map:
            if entry.intent == intent:
                return [self.nodes[n] for n in entry.nodes if n in self.nodes]
        return []

    def gate_for(self, node_name: str) -> NodeGate | None:
        """Get the gate for a node."""
        node = self.nodes.get(node_name)
        return node.gate if node else None

    def all_denied_tools(self, active_nodes: list[str]) -> set[str]:
        """Get the union of denied tools across all active nodes."""
        denied = set()
        for name in active_nodes:
            node = self.nodes.get(name)
            if node and node.gate:
                denied.update(node.gate.denied_tools)
        return denied
```

**关键设计**：

- `WorkflowNode` 不知道自己连着谁——它只声明 gate、workflow、decision_rules
- `Edge` 的 source/target 都是 node name 字符串，不是引用——解耦
- `WorkflowDAG.intent_map` 替代了 `workflow_skill_activations()` 中的硬编码 intent→skill 映射
- `WorkflowDAG.all_denied_tools()` 直接给权限层用，不需要再查 `WORKFLOW_SKILL_GATES`

## 8 个 WorkflowNode 定义

### brainstorming

```python
BRAINSTORMING = WorkflowNode(
    name="brainstorming",
    description="Use before creating features, building components, or modifying behavior. Explores intent, requirements, and design before implementation.",
    triggers=[
        "create feature", "build component", "add functionality", "new feature",
        "design", "brainstorm", "refactor", "restructure",
        "新功能", "实现新功能", "设计", "头脑风暴", "需求澄清", "重构", "重组",
    ],
    priority=5,
    core_rule="Present a design and get user approval before writing any code.",
    gate=NodeGate(
        denied_tools=("write", "edit", "apply_patch", "lsp_format"),
        description="Do not write code, invoke implementation skills, or take implementation action until the design is presented and approved.",
    ),
    workflow=[
        WorkflowStep(1, "Explore context", "Check files, docs, recent commits to understand the current state."),
        WorkflowStep(2, "Ask clarifying questions", "One at a time. Understand purpose, constraints, and success criteria. If the request is ambiguous, ask before assuming."),
        WorkflowStep(3, "Propose 2-3 approaches", "With trade-offs and your recommendation."),
        WorkflowStep(4, "Present design for approval", "Scaled to complexity. Get user approval. If the scope covers multiple independent subsystems, suggest splitting into separate designs."),
        WorkflowStep(5, "Write design doc", "Save to docs/specs/<topic>-design-YYYY-MM-DD.md."),
    ],
    decision_rules=[
        DecisionRule("skip_to_plan", "If the user explicitly says 'just implement it', skip to writing-plans but still confirm the goal in one sentence first."),
        DecisionRule("skip_to_plan", "If the user's request is already a detailed spec with clear requirements, confirm understanding and go directly to writing-plans."),
        DecisionRule("small_change", "For small, well-scoped changes (renaming, adding a config field, fixing a typo), confirm the goal in one sentence and go directly to test-driven-development."),
    ],
    anti_patterns=[
        '"This is too simple to need a design" is where unexamined assumptions cause the most wasted work. The design can be short, but it must be presented and approved.',
    ],
)
```

### writing-design-docs

```python
WRITING_DESIGN_DOCS = WorkflowNode(
    name="writing-design-docs",
    description="Use when writing technical design docs, PRDs, RFCs, API docs, READMEs, or changelogs. Covers both design-phase and post-implementation documentation.",
    triggers=[
        "design doc", "technical design", "architecture doc", "RFC",
        "API doc", "API 文档", "README", "changelog", "release notes",
        "write docs", "document this", "PRD", "product requirements",
        "需求文档", "产品需求", "技术设计", "架构文档", "接口文档", "写文档", "变更日志",
    ],
    priority=25,
    core_rule="Write for the reader who has zero context. If they can't use the doc without asking you questions, the doc isn't done.",
    gate=NodeGate(
        denied_tools=(),
        description="Do not skip the reader test. Every document must pass a fresh-read check before being considered complete.",
    ),
    workflow=[
        WorkflowStep(1, "Identify the document type", "Which type fits? If none fit, define the structure based on the reader's needs."),
        WorkflowStep(2, "Gather context", "Read the relevant code, specs, and existing docs. Do not write from memory alone."),
        WorkflowStep(3, "Write the first draft", "Be concrete: use real paths, real command names, real field names. No placeholders except [TBD]."),
        WorkflowStep(4, "Reader test", "Re-read the document as if you have zero prior context. For every statement that raises a question the doc doesn't answer, add the answer or fix the statement."),
        WorkflowStep(5, "Verify accuracy", "Check that code examples run, paths exist, API shapes match the actual implementation."),
    ],
    extra_sections={
        "Two Scenarios": (
            "### Scenario 1: Design Phase (after brainstorming, before writing-plans)\n"
            "Write technical design docs, PRDs, and RFCs that turn an approved product design into an implementable specification.\n\n"
            "### Scenario 2: Post-Implementation (after verification, before requesting-code-review)\n"
            "Write API docs, READMEs, and changelogs that make the completed work usable by others."
        ),
        "Document Types": (
            "| Type | When to use |\n"
            "|------|------------|\n"
            "| Product Requirements Doc | A product feature needs a structured spec before implementation |\n"
            "| Technical Design Doc | A feature needs architecture decisions before implementation |\n"
            "| RFC / Decision Doc | A significant technical decision needs team alignment |\n"
            "| API Documentation | Implementation is done and others need to integrate |\n"
            "| README / Usage Guide | Implementation is done and others need to use or contribute |\n"
            "| Changelog | Shipping a version or merging to main |"
        ),
        "PRD-Specific Rules": (
            "- Proactively fill blind spots — users without PM training won't think to specify interaction details.\n"
            "- Dual audience — the doc serves both developers and end users.\n"
            "- Merge, don't overwrite — integrate new content, mark changes with 【Updated】."
        ),
        "Principles": (
            "- Write for the reader, not the writer.\n"
            "- Start with the most useful information.\n"
            "- Show, don't tell.\n"
            "- Link, don't duplicate.\n"
            "- Outdated docs are worse than no docs."
        ),
    },
)
```

### writing-plans

```python
WRITING_PLANS = WorkflowNode(
    name="writing-plans",
    description="Use when turning a spec, requirements, or agreed design into an implementation plan before editing code.",
    triggers=[
        "implementation plan", "write a plan", "planning", "spec", "requirements",
        "计划", "实施方案", "需求",
    ],
    priority=30,
    core_rule="Plans must be executable — exact paths, concrete commands, voidx tool names.",
    gate=NodeGate(
        denied_tools=("write", "edit", "apply_patch", "lsp_format"),
        description="Do not start implementation until the plan is approved. The plan must be executable with exact paths and commands.",
    ),
    workflow=[
        WorkflowStep(1, "Define goal", "One sentence."),
        WorkflowStep(2, "Describe architecture", "2-3 sentences about approach."),
        WorkflowStep(3, "List tech stack", "Key technologies and libraries."),
        WorkflowStep(4, "Define file structure", "List files to create or modify, with one-line responsibility per file."),
        WorkflowStep(5, "Write tasks", "Ordered steps with checkboxes. Each step is one action (2-5 minutes)."),
        WorkflowStep(6, "Define tests", "Targeted commands and expected results per task."),
        WorkflowStep(7, "Identify risks", "Edge cases, compatibility, and rollback concerns."),
    ],
    extra_sections={
        "Scope Check": "If the spec covers multiple independent subsystems, suggest splitting into separate plans.",
        "Task Template": (
            "```\n"
            "## Task N: [Component Name]\n\n"
            "**Files:**\n- Modify: `path/to/file.py`\n\n"
            "- [ ] **Step 1:** Write failing test for [behavior]\n"
            "- [ ] **Step 2:** Run test, confirm it fails\n"
            "- [ ] **Step 3:** Implement minimal code to pass\n"
            "- [ ] **Step 4:** Run test, confirm it passes\n"
            "- [ ] **Step 5:** Run broader test set\n"
            "```"
        ),
        "Execution": "After the plan is approved, follow test-driven-development for each task. Before claiming any task complete, follow verification-before-completion.",
    },
)
```

### test-driven-development

```python
TEST_DRIVEN_DEVELOPMENT = WorkflowNode(
    name="test-driven-development",
    description="Use before implementing features, bug fixes, refactors, or behavior changes.",
    triggers=[
        "implement", "feature", "bugfix", "refactor", "behavior change",
        "add support", "fix bug",
        "实现", "修复", "重构", "功能",
    ],
    priority=40,
    core_rule="Write a test that fails for the intended reason before writing the implementation.",
    gate=NodeGate(
        denied_tools=(),
        description="If you wrote implementation code before a failing test, delete the implementation and start from the test. Do not keep it as 'reference.'",
    ),
    workflow=[
        WorkflowStep(1, "Write failing test", "Identify the smallest behavior to prove. One behavior, clear name, real code."),
        WorkflowStep(2, "Verify red", "Run the targeted test and confirm it fails for the expected reason."),
        WorkflowStep(3, "Implement minimal code", "Make the smallest code change that makes the test pass."),
        WorkflowStep(4, "Verify green", "Run the targeted test again and confirm it passes."),
        WorkflowStep(5, "Refactor", "Refactor only after the test is green. Run the test after each refactor step."),
        WorkflowStep(6, "Run broader test set", "Run the relevant broader test set before reporting completion."),
    ],
    allowed_exceptions=[
        "Pure documentation, prompt-only edits, generated assets, or configuration-only changes. If you skip TDD for one of these, say why briefly.",
    ],
)
```

### verification-before-completion

```python
VERIFICATION_BEFORE_COMPLETION = WorkflowNode(
    name="verification-before-completion",
    description="Use before claiming work is complete, fixed, passing, ready, or safe to merge.",
    triggers=[
        "done", "complete", "fixed", "passing", "ready", "verify", "verified",
        "looks good", "should work",
        "完成", "修好了", "通过", "验证", "好了", "没问题了",
    ],
    priority=50,
    core_rule="Evidence before completion claims.",
    gate=NodeGate(
        denied_tools=(),
        description=(
            "Before claiming any status:\n"
            "1. Identify the command or check that proves the claim.\n"
            "2. Run the full relevant command in this turn.\n"
            "3. Read the exit code and failure count.\n"
            "4. If the check fails, report the actual failure and next step.\n"
            "5. If the check passes, report the command and result.\n"
            "Only after step 5 may you make the claim."
        ),
    ),
    extra_sections={
        "Common Failure Modes": (
            "| Claim | Requires | Not Sufficient |\n"
            "|-------|----------|----------------|\n"
            "| Tests pass | Test command output: 0 failures | Previous run, 'should pass' |\n"
            "| Build succeeds | Build command: exit 0 | Linter passing, logs look good |\n"
            "| Bug fixed | Original symptom no longer reproduces | Code changed, assumed fixed |\n"
            "| Agent completed | VCS diff shows changes | Agent reports 'success' |\n"
            "| Requirements met | Line-by-line checklist | Tests passing alone |"
        ),
        "Regression Tests": (
            "For bug fixes with regression tests, verify the red-green cycle:\n"
            "1. Write the test. Run it. It must fail.\n"
            "2. Apply the fix. Run it. It must pass.\n"
            "3. Revert the fix. Run it. It must fail again.\n"
            "4. Restore the fix. Run it. It must pass."
        ),
        "Red Flags": (
            "- Using 'should', 'probably', 'seems to' instead of reporting evidence.\n"
            "- Expressing satisfaction before running verification.\n"
            "- Trusting subagent success reports without independent verification.\n"
            "- Relying on earlier runs or partial checks."
        ),
    },
)
```

### requesting-code-review

```python
REQUESTING_CODE_REVIEW = WorkflowNode(
    name="requesting-code-review",
    description="Use after substantial implementation work, complex bug fixes, or before merging to request a focused review.",
    triggers=[
        "request review", "ask for review", "before merge", "pre-merge",
        "review this change",
        "复核一下", "合并前",
    ],
    priority=60,
    core_rule="Review early, review often.",
    gate=NodeGate(
        denied_tools=(),
        description="Do not merge to main or mark substantial work complete without requesting review.",
    ),
    extra_sections={
        "When to Request": (
            "**Mandatory:**\n"
            "- After completing a major feature.\n"
            "- Before merge to main.\n\n"
            "**Valuable:**\n"
            "- When stuck — a fresh perspective helps.\n"
            "- After fixing a complex bug.\n"
            "- Before refactoring — establish a baseline."
        ),
        "How to Request": (
            "In voidx, request review with `agent(review)` when available.\n\n"
            "Review brief must include:\n"
            "1. What changed.\n"
            "2. Requirements or plan being checked.\n"
            "3. Files changed or relevant diff range.\n"
            "4. Verification already run.\n"
            "5. Specific risks to inspect."
        ),
        "Acting on Feedback": (
            "When review feedback arrives, follow receiving-code-review.\n\n"
            "- Fix correctness, security, and broken behavior before proceeding.\n"
            "- Fix important issues before moving to the next task.\n"
            "- Note minor issues for later.\n"
            "- Push back if the review is wrong and explain the evidence."
        ),
        "Anti-Patterns": (
            "- Skipping review because 'it's simple.'\n"
            "- Ignoring critical issues and proceeding.\n"
            "- Arguing with valid technical feedback."
        ),
    },
)
```

### receiving-code-review

```python
RECEIVING_CODE_REVIEW = WorkflowNode(
    name="receiving-code-review",
    description="Use when receiving review feedback, requested optimizations, or reviewer comments before implementing them.",
    triggers=[
        "review feedback", "code review feedback", "reviewer says",
        "feedback says", "review comment",
        "优化点", "审查意见", "评审意见",
    ],
    priority=20,
    core_rule="Verify feedback against the codebase before changing code.",
    gate=NodeGate(
        denied_tools=(),
        description="Do not implement any feedback item before verifying it against the codebase.",
    ),
    workflow=[
        WorkflowStep(1, "Read the full feedback", ""),
        WorkflowStep(2, "Restate the concrete requested changes", "If needed."),
        WorkflowStep(3, "Check the relevant code and tests", ""),
        WorkflowStep(4, "Decide whether each item is correct", "For this codebase."),
        WorkflowStep(5, "Push back when feedback is wrong", "With technical reasons."),
        WorkflowStep(6, "Implement valid feedback", "One coherent item at a time."),
        WorkflowStep(7, "Verify with targeted tests or commands", "Before reporting."),
    ],
    extra_sections={
        "Source-Specific Rules": (
            "### From the user (your human partner)\n"
            "- Trusted intent — implement after understanding.\n"
            "- Still ask if scope is unclear.\n"
            "- No performative agreement. Skip to action or technical acknowledgment.\n\n"
            "### From external reviewers\n"
            "Before implementing, check:\n"
            "1. Technically correct for this codebase?\n"
            "2. Breaks existing functionality?\n"
            "3. Does the reviewer understand the full context?"
        ),
        "Handling Unclear Feedback": "If any item is unclear, stop and ask for clarification before implementing anything.",
        "YAGNI Check": "If a reviewer suggests 'implementing properly' or adding completeness, check whether the code is actually used.",
    },
)
```

### systematic-debugging

```python
SYSTEMATIC_DEBUGGING = WorkflowNode(
    name="systematic-debugging",
    description="Use when debugging bugs, failed tests, build failures, tracebacks, crashes, or unexpected behavior.",
    triggers=[
        "bug", "failed", "failure", "traceback", "error", "crash",
        "broken", "not working", "unexpected", "test failure", "build failure",
        "报错", "失败", "异常", "崩溃", "排查", "不对", "结果不对",
    ],
    priority=10,
    core_rule="Find the root cause before changing code.",
    gate=NodeGate(
        denied_tools=("write", "edit", "apply_patch", "lsp_format"),
        description="Root cause investigation must complete before proposing any fix. If you catch yourself about to suggest a fix without evidence, stop and gather more data.",
    ),
    workflow=[
        WorkflowStep(1, "Read the full error", "Traceback, logs, or failing assertion. Do not skip warnings or partial output."),
        WorkflowStep(2, "Reproduce the issue", "With the smallest reliable command or steps."),
        WorkflowStep(3, "Check recent changes", "With read-only tools: grep, read, safe bash. Look at git diff, recent commits, config changes."),
        WorkflowStep(4, "Diagnose multi-component systems", "Add diagnostic logging at each component boundary, run once to locate where it breaks."),
        WorkflowStep(5, "Form a concrete hypothesis", "Not 'maybe it's broken' — a specific claim about what is wrong and where."),
        WorkflowStep(6, "Verify the hypothesis", "With a targeted command, diagnostic, or code read."),
        WorkflowStep(7, "Make the smallest fix", "Only then. For non-trivial fixes, follow test-driven-development."),
        WorkflowStep(8, "Run reproduction command again", "Report the evidence."),
        WorkflowStep(9, "Run broader test set", ""),
    ],
    anti_patterns=[
        '"This bug is too simple to need investigation" — simple bugs have root causes too.',
        '"I\'ll just try this fix" — trying fixes is not debugging, it\'s guessing.',
        "Skipping phases under time pressure — rushing guarantees rework.",
    ],
)
```

## WorkflowDAG 定义

所有边和入口映射集中定义在编排层：

```python
# src/voidx/skills/bundled/dag.py

from voidx.skills.schema import Edge, IntentEntry, WorkflowDAG
from voidx.skills.bundled.brainstorming import BRAINSTORMING
from voidx.skills.bundled.writing_design_docs import WRITING_DESIGN_DOCS
from voidx.skills.bundled.writing_plans import WRITING_PLANS
from voidx.skills.bundled.test_driven_development import TEST_DRIVEN_DEVELOPMENT
from voidx.skills.bundled.verification_before_completion import VERIFICATION_BEFORE_COMPLETION
from voidx.skills.bundled.requesting_code_review import REQUESTING_CODE_REVIEW
from voidx.skills.bundled.receiving_code_review import RECEIVING_CODE_REVIEW
from voidx.skills.bundled.systematic_debugging import SYSTEMATIC_DEBUGGING

DEFAULT_WORKFLOW_DAG = WorkflowDAG(
    name="default",
    nodes={
        "brainstorming": BRAINSTORMING,
        "writing-design-docs": WRITING_DESIGN_DOCS,
        "writing-plans": WRITING_PLANS,
        "test-driven-development": TEST_DRIVEN_DEVELOPMENT,
        "verification-before-completion": VERIFICATION_BEFORE_COMPLETION,
        "requesting-code-review": REQUESTING_CODE_REVIEW,
        "receiving-code-review": RECEIVING_CODE_REVIEW,
        "systematic-debugging": SYSTEMATIC_DEBUGGING,
    },
    edges=[
        # brainstorming 出边
        Edge("brainstorming", "writing-design-docs", "approved", "design approved"),
        Edge("brainstorming", "writing-plans", "skip_to_plan", "user says 'just implement it' or spec is detailed"),
        Edge("brainstorming", "test-driven-development", "small_change", "small scoped change"),

        # writing-design-docs 出边
        Edge("writing-design-docs", "writing-plans", "completed", "doc passes reader test"),

        # writing-plans 出边
        Edge("writing-plans", "test-driven-development", "approved", "plan approved"),

        # test-driven-development 出边
        Edge("test-driven-development", "verification-before-completion", "implemented", "implementation complete"),

        # verification-before-completion 出边
        Edge("verification-before-completion", "requesting-code-review", "passed_substantial", "verification passed after substantial work"),
        Edge("verification-before-completion", "test-driven-development", "failed_implementation", "verification failed — implementation issue"),
        Edge("verification-before-completion", "systematic-debugging", "failed_bug", "verification failed — bug found"),

        # requesting-code-review 出边
        Edge("requesting-code-review", "receiving-code-review", "review_has_issues", "review returned FAIL or NEEDS_CHANGE"),

        # receiving-code-review 出边
        Edge("receiving-code-review", "test-driven-development", "feedback_valid", "feedback verified and valid"),
        Edge("receiving-code-review", "verification-before-completion", "feedback_verified", "feedback implemented, needs verification"),

        # systematic-debugging 出边
        Edge("systematic-debugging", "test-driven-development", "nontrivial_fix", "fix requires TDD"),
        Edge("systematic-debugging", "verification-before-completion", "trivial_fix", "fix is trivial"),
    ],
    intent_map=[
        IntentEntry("debug", ["systematic-debugging", "verification-before-completion"], "debug intent"),
        IntentEntry("implement", ["test-driven-development", "verification-before-completion"], "implement intent"),
        IntentEntry("design", ["brainstorming"], "design intent"),
        IntentEntry("review", ["requesting-code-review"], "review intent"),
    ],
)
```

**与之前方案的关键区别**：

- 之前：每个 WorkflowNode 内嵌 `edges`，Node 自己声明出边
- 现在：`WorkflowNode` 不含 `edges`，所有边集中在 `WorkflowDAG.edges` 中
- `WorkflowDAG.intent_map` 替代了 `workflow_skill_activations()` 中的硬编码逻辑
- 新增 Node 只需加到 `nodes` 字典和 `edges` 列表，不需要改已有 Node

## Transition 机制：Node 完成后的出口选择

### 核心流程

```
Node 完成 workflow
    │
    ▼
LLM 选择出口 condition（基于 decision_rules）
    │
    ├─ 不选 / 选 "结束" → 报告用户，Node 生命周期结束
    │
    ├─ 选了某个 condition → 查 DAG.edges_from(node)
    │       │
    │       ├─ 找到匹配的 Edge → 激活 target Node
    │       └─ 找不到 → 也结束，报告用户
    │
    └─ （无出边的 Node → 自动结束）
```

### 设计原则

1. **默认是结束**：Node 完成后如果不选出口，就是结束。不是所有 Node 都必须连到下一个 Node
2. **出口由 LLM 选择**：LLM 根据 `decision_rules` 判断当前满足哪个 condition，然后调用 `advance_workflow` 工具
3. **DAG 验证**：runtime 校验 condition 是否是当前 Node 的合法出边，防止 LLM 乱选
4. **无出边 = 终端节点**：如果 DAG 中某个 Node 没有出边，完成后自动结束

### advance_workflow 工具

```python
# src/voidx/tools/advance_workflow.py

from pydantic import BaseModel, Field


class AdvanceWorkflowInput(BaseModel):
    condition: str = Field(
        description=(
            "The transition condition to take from the current workflow node. "
            "Must match one of the outgoing edge conditions defined in the workflow DAG. "
            "Use 'done' or omit to end the current node without transitioning."
        )
    )


async def advance_workflow(condition: str) -> str:
    """Choose an exit condition from the current workflow node.

    Call this when the current node's workflow is complete and you've
    determined which condition applies based on the decision rules.

    - If condition matches an outgoing edge, the target node is activated.
    - If condition is 'done' or no matching edge exists, the node ends.
    - If the node has no outgoing edges, it ends automatically.
    """
    state = runtime.skill_run_state
    dag = DEFAULT_WORKFLOW_DAG

    # 找到当前活跃的 node
    active_nodes = state.active_node_names()
    if not active_nodes:
        return "No active workflow node to transition from."

    # 默认：结束
    if condition == "done":
        for name in active_nodes:
            state.deactivate(name)
        return "Workflow node completed. No transition."

    # 查 DAG 找匹配的出边
    for node_name in active_nodes:
        edges = dag.edges_from(node_name)
        matched = [e for e in edges if e.condition == condition]
        if matched:
            edge = matched[0]
            target_node = dag.nodes.get(edge.target)
            if target_node:
                state.deactivate(node_name)
                state.activate(edge.target, source="workflow")
                return (
                    f"Transition: {node_name} → {edge.target}\n"
                    f"Condition: {condition} ({edge.label})\n"
                    f"Target node activated: {edge.target}"
                )
            else:
                state.deactivate(node_name)
                return (
                    f"Edge points to '{edge.target}' but no such node in DAG. "
                    f"Node {node_name} completed without transition."
                )

    # 没有匹配的 condition
    available = []
    for node_name in active_nodes:
        for e in dag.edges_from(node_name):
            available.append(f"  {e.condition}: {e.label} → {e.target}")

    if available:
        return (
            f"Invalid condition '{condition}'. Available exits:\n"
            + "\n".join(available)
            + "\nUse 'done' to end without transitioning."
        )
    else:
        return "Current node has no outgoing edges. It ends automatically."
```

### 与 on_intent 的分工

| 工具 | 阶段 | 作用 |
|------|------|------|
| `on_intent` | 进门 | intent → 激活 DAG entry nodes |
| `advance_workflow` | 出门 | condition → 激活 target node |

`on_intent` 管"从哪里进"，`advance_workflow` 管"从哪里出"。两个工具，两个阶段。

### LLM 如何知道有哪些出口

两个渠道：

1. **Skill context 注入**：当前活跃 Node 的 decision_rules + DAG 出边信息注入到 LLM 的 skill context 中
2. **advance_workflow 的错误提示**：如果 LLM 选了不存在的 condition，工具返回可用的出口列表

```python
# 注入到 skill context 的示例
"""
## Current Node: brainstorming

### Decision Rules
- skip_to_plan: If the user explicitly says 'just implement it'...
- small_change: For small, well-scoped changes...

### Available Exits
- approved → writing-design-docs
- skip_to_plan → writing-plans
- small_change → test-driven-development
- done → end
"""
```

### 示例流程

```
用户: "帮我加个配置字段，把 timeout 从 30 改成 60"

LLM 推理:
  → brainstorming 的 decision_rules 说 small_change 适用于小改动
  → 调用 advance_workflow(condition="small_change")

Runtime:
  → brainstorming 有出边 small_change → test-driven-development
  → 激活 test-driven-development
  → 返回 "Transition: brainstorming → test-driven-development"

LLM:
  → 按 TDD 流程执行
  → 完成后调用 advance_workflow(condition="implemented")

Runtime:
  → test-driven-development 有出边 implemented → verification-before-completion
  → 激活 verification-before-completion
  → ...

LLM:
  → 验证通过，这是小改动，不需要 code review
  → 调用 advance_workflow(condition="done")
  → 报告用户完成
```

## Context 层级设计：Node 信息放在哪一层

### 当前 context 层级

| 层 | 消息类型 | 内容 | 变化频率 | 缓存 |
|---|---|---|---|---|
| `sections` (stable) | SystemMessage | Base System, Role Prompt, Mode Prompt, Tool Contract, Workspace Facts, Project Facts, Session Date, Long Summary | 低 | hash 缓存 |
| `skill_context_content` | HumanMessage | 所有 bundled skill 完整 body（参考库） | 中 | hash 缓存 |
| `task_sections` | 拼到 UserMessage 前 | Runtime State, DateTime, Task State | 高 | 无 |

当前所有 bundled skill 的完整 body 都放在 `skill_context_content`（HumanMessage），作为参考库。不管是否激活，全部注入。

### Node 信息的三类

结构化后，Node 信息分为三类，变化频率和用途不同：

| 类型 | 内容 | 变化频率 | token 量 | 用途 |
|------|------|---------|---------|------|
| **A. Node 定义** | gate, workflow, decision_rules, anti_patterns | 极低（随版本发布） | 大（每个 ~200-500 token） | LLM 执行指令 |
| **B. DAG 拓扑** | edges, intent_map | 极低（随版本发布） | 小（~100 token） | LLM 理解全局流程 |
| **C. 运行时状态** | 当前活跃 Node、出边选项、gate 约束 | 高（每 turn 变） | 小（~50 token） | LLM 当前决策 |

### 方案对比

#### 方案 1：全部放 system_prompt（stable sections）

```
SystemMessage:
  Base System
  Role Prompt
  ...
  WorkflowNode Reference Library  ← A + B 全放这里
```

- ✅ 稳定，可缓存，不随 turn 变化
- ✅ LLM 每次都能看到完整 Node 定义
- ❌ token 开销大：8 个 Node 完整定义 ~2000-4000 token，**每轮都占**
- ❌ 不活跃的 Node 也占位，浪费 context window
- ❌ system_prompt 膨胀，影响推理质量

#### 方案 2：全部放 task_sections（每轮注入）

```
UserMessage:
  Current Task State
    - Active Node: brainstorming
    - Available exits: approved → writing-design-docs, skip_to_plan → writing-plans
    - Gate: denied_tools=(write, edit, ...)
    - Decision rules: ...
    - Workflow: 1. Explore context 2. Ask clarifying questions ...
```

- ✅ 只注入当前活跃 Node 的信息，token 省
- ❌ 每轮都变，无法缓存
- ❌ 活跃 Node 的完整 workflow + decision_rules 每轮重复注入
- ❌ 非活跃 Node 的定义 LLM 看不到（无法提前了解流程）

#### 方案 3：分层放置（推荐）

```
SystemMessage (stable, 缓存):
  Base System
  Role Prompt
  ...
  [不变] Workflow DAG Overview  ← B 类：DAG 拓扑摘要

HumanMessage (workflow_node_context, 缓存):  ← 新增独立层，优先级更高
  VOIDX_WORKFLOW_NODE_CONTEXT
  Scope: workflow-node-reference-library
  [不变] 所有 WorkflowNode 的完整定义  ← A 类

HumanMessage (skill_context, 缓存):
  VOIDX_SKILL_CONTEXT
  Scope: bundled-skill-reference-library
  [不变] 所有 skill 的摘要（不含 body，body 已在 workflow_node_context）

UserMessage (task_sections, 每轮):
  Current Task State
    - Active Node: brainstorming (phase=design)
    - Available exits: approved → writing-design-docs | skip_to_plan → writing-plans | small_change → test-driven-development | done → end
    - Gate: denied_tools=(write, edit, apply_patch, lsp_format)
  [每轮变] 运行时状态  ← C 类
```

### 方案 3 详解

#### A 类：Node 定义 → workflow_node_context（新增独立 HumanMessage 层）

**不和 skill_context_content 混放**。Node 定义是结构化 schema，语义不同于 skill 参考库。

新增 `workflow_node_context` 字段到 `RuntimeContext`，作为独立的 HumanMessage 注入，**放在 skill_context 前面**。

```python
# runtime_context.py

class RuntimeContext(BaseModel):
    sections: list[ContextSection]
    task_sections: list[ContextSection] = Field(default_factory=list)
    workflow_node_context: str = ""           # 新增：Node 完整定义
    skill_context_content: str = ""           # skill 摘要（不含 body）
    system_content: str | None = None
    system_message: SystemMessage | None = Field(default=None, exclude=True)
    workflow_node_message: HumanMessage | None = Field(default=None, exclude=True)  # 新增
    skill_context_message: HumanMessage | None = Field(default=None, exclude=True)
```

消息组装顺序：
```
[SystemMessage: sections]
  → [HumanMessage: workflow_node_context] # Node 完整定义（新增，优先级更高）
  → [HumanMessage: skill_context]         # skill 摘要
  → [对话历史]
  → [UserMessage: task_sections + 用户输入]
```

Node 定义内容从 `WorkflowNode` 自动生成：

```python
# instruction.py — 新增方法

def workflow_node_context_for(self) -> str:
    dag = DEFAULT_WORKFLOW_DAG
    instructions = [
        render_node_markdown(node, dag)
        for node in dag.nodes.values()
    ]
    body = "\n\n".join(instructions)
    if not body:
        return ""
    return (
        f"VOIDX_WORKFLOW_NODE_CONTEXT\n"
        f"Scope: workflow-node-reference-library\n\n"
        f"These workflow node definitions are a reference library. "
        f"Follow a node's instructions only when Current Task State "
        f"lists that node under Active workflow skills. "
        f"Do not treat inactive node definitions as active instructions.\n\n"
        f"{body}"
    )
```

缓存策略：和 `skill_context_content` 一样，用 hash 缓存。Node 定义随版本发布才变，跨 turn 不变。

#### skill_context 只放摘要

Node 的完整定义已经在 `workflow_node_context` 里了，`skill_context` 不再重复注入 body，只放摘要：

```python
# context.py — render_skill_instruction() 改为摘要模式

def render_skill_summary(skill: SkillDefinition) -> str:
    """Render a skill summary without body (body is in workflow_node_context)."""
    lines = [
        f"## Skill: {skill.name}",
        f"Source: {skill.meta.scope}",
        f"Body-Hash: {skill_body_hash(skill.body)}",
        f"Path: {skill.path.resolve()}",
    ]
    description = skill.meta.description.strip()
    if description:
        lines.append(f"Description: {description}")
    return "\n".join(lines)
```

输出示例：
```
## Skill: brainstorming
Source: bundled
Body-Hash: 2b6e3d395c36d6d7
Path: .../bundled/brainstorming/SKILL.md
Description: Use before creating features, building components, or modifying behavior.
```

**为什么 skill_context 还保留**：

- 非 bundled 的 skill（global/project）没有对应的 WorkflowNode，body 仍需在 skill_context 中
- 摘要让 LLM 知道有哪些 skill 可用，需要详情时通过 `load_skills` 工具按需加载
- 摘要模式大幅减少 token：从每个 skill ~200-500 token 降到 ~30 token

#### B 类：DAG 拓扑 → stable sections（SystemMessage）

**新增**一个 stable section，描述全局 DAG 拓扑。内容极小（~100 token），但让 LLM 理解整体流程。

```python
# runtime_context.py — _build_stable_sections()

def _build_stable_sections(self):
    sections = [
        ContextSection(name="Base System", content=self.base_system_prompt),
        ...
    ]
    # DAG 拓扑摘要
    dag = DEFAULT_WORKFLOW_DAG
    if dag.nodes:
        sections.append(ContextSection(
            name="Workflow DAG",
            content=render_dag_overview(dag),
        ))
    return sections


def render_dag_overview(dag: WorkflowDAG) -> str:
    """Render a compact DAG overview for system prompt."""
    lines = ["Workflow nodes and transitions:"]
    for name, node in dag.nodes.items():
        edges = dag.edges_from(name)
        if edges:
            exits = ", ".join(f"{e.condition}→{e.target}" for e in edges)
            lines.append(f"  {name}: {exits}")
        else:
            lines.append(f"  {name}: (terminal)")
    lines.append("")
    lines.append("Entry points by intent:")
    for entry in dag.intent_map:
        lines.append(f"  {entry.intent} → {', '.join(entry.nodes)}")
    return "\n".join(lines)
```

输出示例：
```
Workflow nodes and transitions:
  brainstorming: approved→writing-design-docs, skip_to_plan→writing-plans, small_change→test-driven-development
  writing-design-docs: completed→writing-plans
  writing-plans: approved→test-driven-development
  test-driven-development: implemented→verification-before-completion
  verification-before-completion: passed_substantial→requesting-code-review, failed_implementation→test-driven-development, failed_bug→systematic-debugging
  requesting-code-review: review_has_issues→receiving-code-review
  receiving-code-review: feedback_valid→test-driven-development, feedback_verified→verification-before-completion
  systematic-debugging: nontrivial_fix→test-driven-development, trivial_fix→verification-before-completion

Entry points by intent:
  debug → systematic-debugging, verification-before-completion
  implement → test-driven-development, verification-before-completion
  design → brainstorming
  review → requesting-code-review
```

~100 token，放在 system prompt 里几乎无开销，但 LLM 能理解全局流程。

#### C 类：运行时状态 → task_sections（UserMessage，每轮）

**增强** `Current Task State`，加入当前活跃 Node 的出边和 gate 约束。

```python
# runtime_context.py — _current_task_state()

def _current_task_state(self):
    lines = [
        f"- Mode: {self.interaction_mode.value}",
        f"- Intent: {self.task_intent.value}",
        f"- Agent: {self.agent}",
        f"- Agent ID: {self.agent_id}",
    ]
    if self.skill_runs:
        lines.append(f"- Skill run state: {'; '.join(run.state_summary() for run in self.skill_runs)}")
        # 新增：当前活跃 Node 的出边和 gate
        dag = DEFAULT_WORKFLOW_DAG
        for run in self.skill_runs:
            if run.status == "active":
                edges = dag.edges_from(run.name)
                node = dag.nodes.get(run.name)
                if edges:
                    exits = " | ".join(f"{e.condition}→{e.target}" for e in edges) + " | done→end"
                    lines.append(f"  Available exits ({run.name}): {exits}")
                if node and node.gate and node.gate.denied_tools:
                    lines.append(f"  Gate ({run.name}): denied_tools={node.gate.denied_tools}")
    ...
```

输出示例：
```
- Skill run state: brainstorming=active phase=design
  Available exits (brainstorming): approved→writing-design-docs | skip_to_plan→writing-plans | small_change→test-driven-development | done→end
  Gate (brainstorming): denied_tools=('write', 'edit', 'apply_patch', 'lsp_format')
```

### 为什么这样分层

| 问题 | 回答 |
|------|------|
| 为什么 Node 定义不放 system_prompt？ | 8 个 Node ~2000-4000 token，system_prompt 每轮都占，且大部分 Node 当前不活跃 |
| 为什么 workflow_node_context 放 skill_context 前面？ | Node 定义是结构化的、优先级更高，skill 摘要是补充 |
| 为什么 DAG 拓扑放 system_prompt？ | ~100 token，几乎无开销，但让 LLM 理解全局流程，知道"下一步可以去哪" |
| 为什么运行时状态放 task_sections？ | 每轮都变，不能缓存，但信息量小（~50 token），放 UserMessage 前缀 |
| 为什么 skill_context 只放摘要？ | 完整定义已在 workflow_node_context，重复注入浪费 token。摘要 ~30 token/skill vs 完整 ~200-500 token/skill |
| 非 bundled skill 的 body 怎么办？ | global/project skill 没有对应 WorkflowNode，body 仍在 skill_context 中完整注入 |

### 未来优化：按需加载

当前 workflow_node_context 把所有 Node 定义都注入。未来可以优化为只注入活跃 Node 的定义：

```python
# 未来优化：只注入活跃 Node
active_names = {run.name for run in self.skill_runs if run.status == "active"}
instructions = [
    render_node_markdown(node, dag)
    for name, node in dag.nodes.items()
    if name in active_names
]
```

这会把 workflow_node_context 的 token 开销从 ~2000-4000 降到 ~200-500（只注入 1-2 个活跃 Node）。
但第一期先保持全量注入，确保行为一致。

## 自动生成 SKILL.md

结构化定义是 single source of truth。SKILL.md 从 `WorkflowNode` 自动生成，供 LLM 阅读：

```python
# src/voidx/skills/render.py

def render_node_markdown(node: WorkflowNode, dag: WorkflowDAG | None = None) -> str:
    """Render a WorkflowNode into the SKILL.md format that LLMs read.

    dag is optional — when provided, transitions are injected from DAG edges,
    since the node itself doesn't know its outgoing edges.
    """
    sections = []

    # Frontmatter
    frontmatter_lines = [f"name: {node.name}"]
    if node.description:
        frontmatter_lines.append(f"description: {node.description}")
    if node.triggers:
        frontmatter_lines.append("triggers:")
        for trigger in node.triggers:
            frontmatter_lines.append(f"  - {trigger}")
    frontmatter = "\n".join(frontmatter_lines)

    # Title + core rule
    body_lines = [f"# {node.name.replace('-', ' ').title()} for voidx", ""]
    if node.description:
        body_lines.append(node.description)
        body_lines.append("")
    if node.core_rule:
        body_lines.append(f"Core rule: {node.core_rule}")
        body_lines.append("")

    # Gate
    if node.gate:
        body_lines.append("## Gate")
        body_lines.append("")
        body_lines.append(node.gate.description)
        body_lines.append("")

    # Anti-patterns
    if node.anti_patterns:
        body_lines.append("## Anti-Patterns")
        body_lines.append("")
        for ap in node.anti_patterns:
            body_lines.append(f"- {ap}")
        body_lines.append("")

    # Workflow
    if node.workflow:
        body_lines.append("## Workflow")
        body_lines.append("")
        for step in node.workflow:
            desc = f" — {step.description}" if step.description else ""
            body_lines.append(f"{step.order}. **{step.action}**{desc}")
        body_lines.append("")

    # Decision rules
    if node.decision_rules:
        body_lines.append("## Decision Rules")
        body_lines.append("")
        for rule in node.decision_rules:
            body_lines.append(f"- {rule.description}")
        body_lines.append("")

    # Allowed exceptions
    if node.allowed_exceptions:
        body_lines.append("## Allowed Exceptions")
        body_lines.append("")
        for exc in node.allowed_exceptions:
            body_lines.append(f"- {exc}")
        body_lines.append("")

    # Extra sections
    for title, content in node.extra_sections.items():
        body_lines.append(f"## {title}")
        body_lines.append("")
        body_lines.append(content)
        body_lines.append("")

    body = "\n".join(body_lines)
    return f"---\n{frontmatter}\n---\n\n{body}\n"
```

## 与现有架构的集成

### SkillRegistry 改造

```python
# src/voidx/skills/registry.py

class SkillRegistry:
    def discover(self) -> list[SkillDefinition]:
        skills: dict[str, SkillDefinition] = {}
        # Bundled skills: 从 WorkflowNode + WorkflowDAG 加载
        dag = DEFAULT_WORKFLOW_DAG
        for name, node in dag.nodes.items():
            skill = _node_to_definition(node, dag, self.bundled_dir)
            skills[normalize_skill_name(node.name)] = skill
        # Global/project skills: 仍从 SKILL.md 加载
        for scope, root in (("global", self.global_dir), ("project", self.project_dir)):
            for skill in self._discover_root(root, scope):
                skills[normalize_skill_name(skill.name)] = skill
        return sorted(skills.values(), key=lambda item: item.name)
```

### policy.py 简化

`WORKFLOW_SKILL_TRANSITIONS` 和 `WORKFLOW_SKILL_PRIORITY` 不再需要手动维护，从 `WorkflowNode` 自动推导：

```python
# src/voidx/skills/policy.py

def _dag_from_workflow() -> dict[str, list[Edge]]:
    """Build the transition map from WorkflowDAG."""
    dag = DEFAULT_WORKFLOW_DAG
    return {name: dag.edges_from(name) for name in dag.nodes if dag.edges_from(name)}

def _priorities_from_workflow() -> dict[str, int]:
    dag = DEFAULT_WORKFLOW_DAG
    return {name: node.priority for name, node in dag.nodes.items()}

def _gates_from_workflow() -> dict[str, NodeGate]:
    dag = DEFAULT_WORKFLOW_DAG
    return {name: node.gate for name, node in dag.nodes.items() if node.gate}
```

### WorkflowDAG 注册

```python
# src/voidx/skills/bundled/__init__.py

from voidx.skills.bundled.brainstorming import BRAINSTORMING
from voidx.skills.bundled.writing_design_docs import WRITING_DESIGN_DOCS
from voidx.skills.bundled.writing_plans import WRITING_PLANS
from voidx.skills.bundled.test_driven_development import TEST_DRIVEN_DEVELOPMENT
from voidx.skills.bundled.verification_before_completion import VERIFICATION_BEFORE_COMPLETION
from voidx.skills.bundled.requesting_code_review import REQUESTING_CODE_REVIEW
from voidx.skills.bundled.receiving_code_review import RECEIVING_CODE_REVIEW
from voidx.skills.bundled.systematic_debugging import SYSTEMATIC_DEBUGGING

BUNDLED_WORKFLOW_NODES = {
    "brainstorming": BRAINSTORMING,
    "writing-design-docs": WRITING_DESIGN_DOCS,
    "writing-plans": WRITING_PLANS,
    "test-driven-development": TEST_DRIVEN_DEVELOPMENT,
    "verification-before-completion": VERIFICATION_BEFORE_COMPLETION,
    "requesting-code-review": REQUESTING_CODE_REVIEW,
    "receiving-code-review": RECEIVING_CODE_REVIEW,
    "systematic-debugging": SYSTEMATIC_DEBUGGING,
}
```

## 向后兼容

| 场景 | 处理方式 |
|------|---------|
| Bundled skill | 从 `WorkflowNode` Python 定义加载，SKILL.md 自动生成 |
| Global skill (`~/.voidx/skills/`) | 仍从 SKILL.md 加载，无 gate/edges（runtime 不强制） |
| Project skill (`.voidx/skills/`) | 仍从 SKILL.md 加载，无 gate/edges（runtime 不强制） |
| `SkillDefinition.body` | 对 bundled skill，从 `WorkflowNode` 自动生成；对 user skill，仍读 SKILL.md |
| `SkillDefinition.meta` | 对 bundled skill，从 `WorkflowNode` 推导；对 user skill，仍读 frontmatter |

## 修改清单

| # | 文件 | 修改内容 | 优先级 |
|---|------|---------|--------|
| 1 | `src/voidx/skills/schema.py` | 新增 `NodeGate`, `Edge`, `DecisionRule`, `WorkflowStep`, `WorkflowNode`, `IntentEntry`, `WorkflowDAG` | P0 |
| 2 | `src/voidx/skills/bundled/brainstorming.py` | 结构化定义 | P0 |
| 3 | `src/voidx/skills/bundled/writing_design_docs.py` | 结构化定义 | P0 |
| 4 | `src/voidx/skills/bundled/writing_plans.py` | 结构化定义 | P0 |
| 5 | `src/voidx/skills/bundled/test_driven_development.py` | 结构化定义 | P0 |
| 6 | `src/voidx/skills/bundled/verification_before_completion.py` | 结构化定义 | P0 |
| 7 | `src/voidx/skills/bundled/requesting_code_review.py` | 结构化定义 | P0 |
| 8 | `src/voidx/skills/bundled/receiving_code_review.py` | 结构化定义 | P0 |
| 9 | `src/voidx/skills/bundled/systematic_debugging.py` | 结构化定义 | P0 |
| 10 | `src/voidx/skills/bundled/dag.py` | 新增 `DEFAULT_WORKFLOW_DAG` 定义 | P0 |
| 11 | `src/voidx/skills/bundled/__init__.py` | 注册 `BUNDLED_WORKFLOW_NODES` | P0 |
| 12 | `src/voidx/tools/advance_workflow.py` | 新增 `advance_workflow` 工具（出口选择） | P0 |
| 13 | `src/voidx/skills/render.py` | 新增 `render_node_markdown(node, dag)` | P1 |
| 14 | `src/voidx/skills/registry.py` | bundled skill 从 `WorkflowNode` + `WorkflowDAG` 加载 | P1 |
| 15 | `src/voidx/skills/policy.py` | DAG/gate/priority 从 `WorkflowDAG` 推导 | P1 |
| 16 | `src/voidx/skills/context.py` | `render_skill_instruction()` 适配 `WorkflowNode`，注入出边信息 | P1 |
| 17 | `src/voidx/skills/bundled/*/SKILL.md` | 改为自动生成（或删除，由 render 动态生成） | P2 |

## 风险

| 风险 | 缓解 |
|------|------|
| 结构化定义丢失 markdown 的表达力 | `extra_sections` 和 `gate.description` 保留自由文本，LLM 仍可读到完整指导 |
| 自动生成的 SKILL.md 与手写版有差异 | 生成后 diff 对比，确保语义等价 |
| Global/project skill 无法使用 gate/edges | 第一期只对 bundled skill 结构化，user skill 保持 SKILL.md 格式 |
| WorkflowNode 字段过多增加维护成本 | 字段都有默认值，简单 node 只需 name + description |
| WorkflowDAG 与 Node 不同步 | DAG 引用不存在的 node name 时启动报错，用 Pydantic validator 校验 |
| LLM 不调用 advance_workflow | Node 完成后 LLM 忘记选出口 → 默认结束，行为安全但可能跳过后续流程 |
| LLM 选错 condition | advance_workflow 返回可用出口列表，LLM 可重试 |
| 多个活跃 Node 同时存在 | advance_workflow 只处理第一个匹配的，避免歧义 |
