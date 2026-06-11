"""Structured definitions for built-in workflow nodes."""

from __future__ import annotations

from voidx.workflow.schema import DecisionRule, NodeGate, TerminalExit, WorkflowNode, WorkflowStep


_TERMINAL_CONDITION = TerminalExit().condition


BRAINSTORMING = WorkflowNode(
    name="brainstorming",
    description=(
        "Use before creating features, building components, or modifying behavior. "
        "Explores intent, requirements, and design before implementation."
    ),
    triggers=[
        "create feature",
        "build component",
        "add functionality",
        "new feature",
        "design",
        "brainstorm",
        "refactor",
        "restructure",
        "新功能",
        "实现新功能",
        "设计",
        "头脑风暴",
        "需求澄清",
        "重构",
        "重组",
    ],
    priority=5,
    core_rule="Present a design and get user approval before writing any code.",
    gate=NodeGate(
        denied_tools=("write", "edit", "apply_patch", "lsp_format"),
        description=(
            "Do not write code, invoke implementation workflows, or take implementation "
            "action until the design is presented and approved. This applies regardless "
            "of perceived simplicity."
        ),
        required_before_transition="design approved by user",
    ),
    workflow=[
        WorkflowStep(order=1, action="Explore context", description="Check files, docs, recent commits to understand the current state."),
        WorkflowStep(order=2, action="Ask clarifying questions", description="One at a time. Understand purpose, constraints, and success criteria."),
        WorkflowStep(order=3, action="Propose 2-3 approaches", description="With trade-offs and your recommendation."),
        WorkflowStep(order=4, action="Present design for approval", description="Scale to complexity and get user approval."),
        WorkflowStep(order=5, action="Write design doc", description="Save to docs/specs/<topic>-design-YYYY-MM-DD.md when needed."),
    ],
    decision_rules=[
        DecisionRule(condition="small_change", description="Highest priority when the change is local/mechanical and does not need a plan: renaming, adding a config field, or fixing a typo goes directly to test-driven-development."),
        DecisionRule(condition="skip_to_plan", description="Use only when the user explicitly says 'just implement it' but the work is not a local/mechanical small_change; confirm the goal in one sentence first."),
        DecisionRule(condition="skip_to_plan", description="Use only when the request is already a detailed spec with clear requirements and is not a local/mechanical small_change; confirm understanding and go directly to writing-plans."),
        DecisionRule(condition="approved", description="For large refactors or behavior changes, continue through design docs after explicit user approval."),
    ],
    anti_patterns=[
        '"This is too simple to need a design" is where unexamined assumptions cause the most wasted work.',
    ],
)


WRITING_DESIGN_DOCS = WorkflowNode(
    name="writing-design-docs",
    description=(
        "Use when writing technical design docs, PRDs, RFCs, API docs, READMEs, "
        "or changelogs. Covers both design-phase and post-implementation documentation."
    ),
    triggers=[
        "design doc",
        "technical design",
        "architecture doc",
        "RFC",
        "API doc",
        "API 文档",
        "README",
        "changelog",
        "release notes",
        "write docs",
        "document this",
        "PRD",
        "product requirements",
        "需求文档",
        "产品需求",
        "技术设计",
        "架构文档",
        "接口文档",
        "写文档",
        "变更日志",
    ],
    priority=25,
    core_rule="Write for the reader who has zero context.",
    gate=NodeGate(
        description="Do not skip the reader test. Every document must pass a fresh-read check before being considered complete.",
        required_before_transition="doc passes reader test",
    ),
    workflow=[
        WorkflowStep(order=1, action="Identify the scenario", description="Design-phase document or post-implementation documentation."),
        WorkflowStep(order=2, action="Identify the document type", description="Pick the structure that fits the reader's needs."),
        WorkflowStep(order=3, action="Gather context", description="Read relevant code, specs, and existing docs. Do not write from memory alone."),
        WorkflowStep(order=4, action="Load the template", description="Use load_doc_template to load the template for the document type."),
        WorkflowStep(order=5, action="Write the first draft", description="Use real paths, commands, and field names. Use [TBD] only when information is missing."),
        WorkflowStep(order=6, action="Reader test", description="Re-read as a fresh reader and fill any gaps."),
        WorkflowStep(order=7, action="Verify accuracy", description="Check examples, paths, and API shapes against the actual implementation."),
    ],
    extra_sections={
        "Two Scenarios": (
            "- Design-phase docs explain intended behavior, options, trade-offs, and implementation plan.\n"
            "- Post-implementation docs explain shipped behavior, exact usage, APIs, and operational notes."
        ),
        "PRD-Specific Rules": (
            "- State user problem, goals, non-goals, acceptance criteria, and launch constraints.\n"
            "- Keep implementation details out unless they affect product behavior."
        ),
        "Document Types": (
            "| Type | doc_type | When to use |\n"
            "|------|----------|-------------|\n"
            "| Product Requirements Doc | `prd` | A product feature needs a structured spec before implementation |\n"
            "| Technical Design Doc | `tech-design` | A feature needs architecture decisions before implementation |\n"
            "| RFC / Decision Doc | `rfc` | A significant technical decision needs team alignment |\n"
            "| API Documentation | `api-doc` | Implementation is done and others need to integrate |\n"
            "| README / Usage Guide | `readme` | Implementation is done and others need to use or contribute |"
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


WRITING_PLANS = WorkflowNode(
    name="writing-plans",
    description="Use when turning a spec, requirements, or agreed design into an implementation plan before editing code.",
    triggers=["implementation plan", "write a plan", "planning", "spec", "requirements", "计划", "实施方案", "需求"],
    priority=30,
    core_rule="Plans must be executable: exact paths, concrete commands, and voidx tool names.",
    gate=NodeGate(
        denied_tools=("write", "edit", "apply_patch", "lsp_format"),
        description="Do not start implementation until the plan is approved. The plan must be executable with exact paths and commands.",
        required_before_transition="plan is executable and approved",
    ),
    workflow=[
        WorkflowStep(order=1, action="Define goal", description="One sentence."),
        WorkflowStep(order=2, action="Describe architecture", description="2-3 sentences about the approach."),
        WorkflowStep(order=3, action="List tech stack", description="Key technologies and libraries."),
        WorkflowStep(order=4, action="Define file structure", description="Files to create or modify, with one-line responsibility per file."),
        WorkflowStep(order=5, action="Write tasks", description="Ordered steps with checkboxes; each step is one small action."),
        WorkflowStep(order=6, action="Define tests", description="Targeted commands and expected results per task."),
        WorkflowStep(order=7, action="Identify risks", description="Edge cases, compatibility, and rollback concerns."),
    ],
    extra_sections={
        "Execution": "After the plan is approved, follow test-driven-development for each task. Before claiming any task complete, follow verification-before-completion.",
    },
)


TEST_DRIVEN_DEVELOPMENT = WorkflowNode(
    name="test-driven-development",
    description="Use before implementing features, bug fixes, refactors, or behavior changes.",
    triggers=["implement", "feature", "bugfix", "refactor", "behavior change", "add support", "fix bug", "实现", "修复", "重构", "功能"],
    priority=40,
    core_rule="Write a test that fails for the intended reason before writing the implementation.",
    gate=NodeGate(
        description="If you wrote implementation code before a failing test, delete the implementation and start from the test.",
        required_before_transition="test written, red verified, implementation green",
    ),
    workflow=[
        WorkflowStep(order=1, action="Write failing test", description="Identify the smallest behavior to prove."),
        WorkflowStep(order=2, action="Verify red", description="Run the targeted test and confirm it fails for the expected reason."),
        WorkflowStep(order=3, action="Implement minimal code", description="Make the smallest code change that makes the test pass."),
        WorkflowStep(order=4, action="Verify green", description="Run the targeted test again and confirm it passes."),
        WorkflowStep(order=5, action="Refactor", description="Refactor only after the test is green."),
        WorkflowStep(order=6, action="Run broader test set", description="Run the relevant broader test set before reporting completion."),
    ],
    allowed_exceptions=[
        "Pure documentation, prompt-only edits, generated assets, or configuration-only changes. If you skip TDD, say why briefly.",
    ],
)


VERIFICATION_BEFORE_COMPLETION = WorkflowNode(
    name="verification-before-completion",
    description="Use before claiming work is complete, fixed, passing, ready, or safe to merge.",
    triggers=["done", "complete", "fixed", "passing", "ready", "verify", "verified", "looks good", "should work", "完成", "修好了", "通过", "验证", "好了", "没问题了"],
    priority=50,
    core_rule="Evidence before completion claims.",
    gate=NodeGate(
        description="Before claiming any status, identify the proving command, run it in this turn, read the output, and report the evidence.",
        required_before_transition="verification command run with evidence",
    ),
    decision_rules=[
        DecisionRule(condition=_TERMINAL_CONDITION, description=f"Verification passed but the change is small or routine — no code review needed. Use '{_TERMINAL_CONDITION}' instead of 'passed_substantial'."),
    ],
    extra_sections={
        "Regression Tests": (
            "For regression claims, prove the test catches the old behavior: write or identify the test, "
            "confirm it fails without the fix when practical, restore the fix, then confirm it passes."
        ),
        "Common Failure Modes": (
            "| Claim | Requires | Not Sufficient |\n"
            "|-------|----------|----------------|\n"
            "| Tests pass | Test command output: 0 failures | Previous run, 'should pass' |\n"
            "| Build succeeds | Build command: exit 0 | Linter passing, logs look good |\n"
            "| Bug fixed | Original symptom no longer reproduces | Code changed, assumed fixed |"
        ),
        "Red Flags": (
            "- Using 'should', 'probably', or 'seems to'.\n"
            "- Relying on earlier runs or partial checks.\n"
            "- Claiming completion before reading command output.\n"
            "- Trusting generated reports without checking the actual diff and test output."
        ),
    },
)


REQUESTING_CODE_REVIEW = WorkflowNode(
    name="requesting-code-review",
    description="Use after substantial implementation work, complex bug fixes, or before merging to request a focused review.",
    triggers=["request review", "ask for review", "before merge", "pre-merge", "review this change", "复核一下", "合并前"],
    priority=60,
    core_rule="Review early, review often.",
    gate=NodeGate(
        description="Do not merge to main or mark substantial work complete without requesting review.",
        required_before_transition="review requested with required brief fields",
    ),
    anti_patterns=[
        "Do not ask for review with only 'please review'. Include context, verification, and risk areas.",
    ],
    extra_sections={
        "When to Request": (
            "- Substantial implementation work.\n"
            "- Complex bug fixes or risky refactors.\n"
            "- Before merge or release when review would catch integration mistakes."
        ),
        "How to Request": (
            "Include what changed, requirements checked, files changed, verification run, "
            "and specific risks to inspect."
        ),
        "Review Brief": "Include what changed, requirements checked, files changed, verification run, and specific risks to inspect.",
        "Acting on Feedback": "When review feedback arrives, follow receiving-code-review.",
    },
)


RECEIVING_CODE_REVIEW = WorkflowNode(
    name="receiving-code-review",
    description="Use when receiving review feedback, requested optimizations, or reviewer comments before implementing them.",
    triggers=["review feedback", "code review feedback", "reviewer says", "feedback says", "review comment", "优化点", "审查意见", "评审意见"],
    priority=20,
    core_rule="Verify feedback against the codebase before changing code.",
    gate=NodeGate(
        description="Do not implement any feedback item before verifying it against the codebase.",
        required_before_transition="feedback verified against codebase",
    ),
    workflow=[
        WorkflowStep(order=1, action="Read the full feedback"),
        WorkflowStep(order=2, action="Restate concrete changes", description="If needed."),
        WorkflowStep(order=3, action="Check code and tests"),
        WorkflowStep(order=4, action="Decide correctness", description="For this codebase."),
        WorkflowStep(order=5, action="Push back when wrong", description="With technical reasons."),
        WorkflowStep(order=6, action="Implement valid feedback", description="One coherent item at a time."),
        WorkflowStep(order=7, action="Verify", description="Run targeted tests or commands before reporting."),
    ],
    extra_sections={
        "Source-Specific Rules": (
            "- Human partner feedback is trusted after understanding; still clarify unclear scope.\n"
            "- External reviewer feedback must be verified against this codebase before implementation."
        ),
        "Handling Unclear Feedback": "If any item is unclear, clarify before implementing the batch.",
        "YAGNI Check": "If feedback asks to implement a 'proper' feature, grep for actual usage first; remove unused paths instead of expanding them.",
    },
)


SYSTEMATIC_DEBUGGING = WorkflowNode(
    name="systematic-debugging",
    description="Use when debugging bugs, failed tests, build failures, tracebacks, crashes, or unexpected behavior.",
    triggers=["bug", "failed", "failure", "traceback", "error", "crash", "broken", "not working", "unexpected", "test failure", "build failure", "报错", "失败", "异常", "崩溃", "排查", "不对", "结果不对"],
    priority=10,
    core_rule="Find the root cause before changing code.",
    gate=NodeGate(
        denied_tools=("write", "edit", "apply_patch", "lsp_format"),
        description="Root cause investigation must complete before proposing or applying any fix.",
        required_before_transition="root cause identified with evidence",
    ),
    workflow=[
        WorkflowStep(order=1, action="Phase 1: Root cause investigation", description="Read the full error, reproduce consistently, check recent changes, and trace data flow."),
        WorkflowStep(order=2, action="Phase 2: Pattern analysis", description="Find working examples and compare differences."),
        WorkflowStep(order=3, action="Phase 3: Hypothesis and testing", description="Form one concrete hypothesis and test it minimally."),
        WorkflowStep(order=4, action="Phase 4: Implementation", description="Make the smallest fix only after root cause is known."),
        WorkflowStep(order=5, action="Run reproduction command again"),
        WorkflowStep(order=6, action="Run broader test set"),
    ],
    anti_patterns=[
        '"I will just try this fix" is guessing, not debugging.',
        "Skipping root-cause investigation guarantees rework.",
    ],
    extra_sections={
        "Flaky or Non-Reproducible Failures": (
            "Do not guess. Gather more observations, isolate variables, add diagnostics at component boundaries, "
            "and only change code after a supported hypothesis exists."
        ),
        "Four Phases": "Root cause investigation -> Pattern analysis -> Hypothesis and testing -> Implementation.",
    },
)


BUILTIN_WORKFLOW_NODES = [
    BRAINSTORMING,
    WRITING_DESIGN_DOCS,
    WRITING_PLANS,
    TEST_DRIVEN_DEVELOPMENT,
    VERIFICATION_BEFORE_COMPLETION,
    REQUESTING_CODE_REVIEW,
    RECEIVING_CODE_REVIEW,
    SYSTEMATIC_DEBUGGING,
]
