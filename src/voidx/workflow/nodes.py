"""Structured definitions for built-in workflow nodes."""

from __future__ import annotations

from voidx.workflow.schema import NodeGate, NodeIO, NodeSubworkflow, WorkflowNode, WorkflowStep


BRAINSTORMING = WorkflowNode(
    name="brainstorm",
    goal="Confirm requirements and design, get user approval",
    description=(
        "Use before creating features, building components, or modifying behavior. "
        "Explores intent, requirements, and design before implementation."
    ),
    persona="explore",
    io=NodeIO(
        input={"user_request": "User's original request"},
        output={
            "design": "Approved design or confirmed change scope",
            "scope": "Confirmed change boundaries",
        },
    ),
    gate=NodeGate(
        description=(
            "Do not write code, invoke implementation workflows, or take implementation "
            "action until the design is presented and approved."
        ),
        required_before_transition="design approved by user",
    ),
    workflow=[
        WorkflowStep(order=1, action="Explore context", description="Check files, docs, recent commits to understand the current state."),
        WorkflowStep(order=2, action="Ask clarifying questions", description="One at a time. Understand purpose, constraints, and success criteria."),
        WorkflowStep(order=3, action="Propose approaches", description="Present trade-offs and a recommendation."),
        WorkflowStep(order=4, action="Present design for approval", description="Scale to complexity and get user approval."),
    ],
    rules=[
        "Do not write the design document inside brainstorm; transition to design if a document is needed.",
    ],
)


WRITING_DESIGN_DOCS = WorkflowNode(
    name="design",
    goal="Produce a document that passes its audience-specific quality gate",
    description=(
        "Use when writing technical design docs, PRDs, RFCs, API docs, READMEs, "
        "implementation specs, implementation task lists, capability specs, or changelogs. Covers "
        "human-facing, LLM-facing, and mixed-audience documentation."
    ),
    persona="plan",
    io=NodeIO(
        input={
            "design": "Approved design or documentation request",
            "audience": "Target document audience: human, llm, or human+llm",
            "action": "Document tool action (list/read)",
        },
        output={
            "doc_path": "Document save path",
            "audience": "Resolved target audience",
            "action": "Actual document tool action",
        },
    ),
    gate=NodeGate(
        description=(
            "Do not skip the audience-specific quality gate. Human-facing docs must "
            "pass a fresh-reader clarity check; LLM-facing specs must pass an "
            "execution-readiness check; mixed docs must pass both."
        ),
        required_before_transition="doc passes audience-appropriate quality gate",
    ),
    workflow=[
        WorkflowStep(order=1, action="Identify the scenario", description="Design-phase document, post-implementation documentation, capability spec, or execution spec."),
        WorkflowStep(order=2, action="Identify the audience", description="Classify the target reader as human, llm, or human+llm before choosing structure or detail level."),
        WorkflowStep(order=3, action="Select the document structure", description="Use a project-provided template when available; otherwise choose a structure suited to the document type and audience."),
        WorkflowStep(order=4, action="Gather context", description="Read relevant code, specs, commands, APIs, and existing docs. Do not write from memory alone."),
        WorkflowStep(order=5, action="Draft for the audience", description="Human docs optimize for clear decisions; LLM specs optimize for paths, invariants, forbidden changes, test commands, and acceptance criteria."),
        WorkflowStep(order=6, action="Fresh reader test", description="For human-facing sections, re-read as a zero-context reader and remove ambiguity, excess detail, and missing conclusions."),
        WorkflowStep(order=7, action="Execution readiness test", description="For LLM-facing sections, verify source paths, current behavior, target behavior, constraints, forbidden changes, and test commands are explicit."),
        WorkflowStep(order=8, action="Verify accuracy", description="Check examples, paths, commands, field names, API shapes, and test instructions against the actual implementation."),
    ],
    rules=[
        "Human-facing docs should optimize for purpose, clarity, readability, and brevity.",
        "LLM-facing specs may be more explicit and repetitive when that improves implementation quality.",
        "Do not put implementation task detail into human docs unless the selected template explicitly asks for it.",
        "Do not leave LLM-facing specs without source paths, invariants, forbidden changes, and verification commands.",
        "Start with the most useful information and link instead of duplicating existing source material.",
    ],
)


WRITING_PLANS = WorkflowNode(
    name="plan",
    goal="Produce an executable implementation plan, get user approval",
    description="Use when turning a spec, requirements, or agreed design into an implementation plan before editing code.",
    persona="plan",
    io=NodeIO(
        input={
            "spec": "Design document or requirements spec",
            "scope": "Change scope",
        },
        output={
            "plan": "Implementation plan with task list, file structure, test definitions",
            "tasks": "Ordered task list",
            "test_commands": "Related verification commands",
        },
    ),
    gate=NodeGate(
        description=(
            "Do not write implementation code until the plan is approved. "
            "Obey the current interaction mode's write permissions."
        ),
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
        WorkflowStep(order=8, action="Verify plan is executable", description="Every task has a file path and a test command."),
    ],
    rules=[
        "Plans must be executable: exact paths, concrete commands, and available tool names.",
        "After the plan is approved, follow tdd for each task.",
    ],
)


TEST_DRIVEN_DEVELOPMENT = WorkflowNode(
    name="tdd",
    goal="Complete implementation via TDD cycle, all tests green",
    description="Use before implementing features, bug fixes, refactors, or behavior changes.",
    persona="implement",
    io=NodeIO(
        input={
            "plan": "Implementation plan",
            "task": "Current task to implement",
        },
        output={
            "files_changed": "List of changed files",
            "tests_written": "List of tests written",
            "test_result": "Test run result",
        },
    ),
    gate=NodeGate(
        description="If you wrote implementation code before a failing test, delete the implementation and start from the test.",
        required_before_transition="test written, red verified, implementation green",
    ),
    workflow=[
        WorkflowStep(order=1, action="Run the internal TDD cycle"),
        WorkflowStep(order=2, action="Confirm implementation output", description="List files changed, tests written, and targeted test result."),
    ],
    subworkflow=NodeSubworkflow(
        name="TDD Cycle",
        description="Repeat until all implementation tasks are complete.",
        steps=[
            WorkflowStep(order=1, action="Pick the next unimplemented requirement or plan task"),
            WorkflowStep(order=2, action="Write a failing test"),
            WorkflowStep(order=3, action="Run the test and confirm RED"),
            WorkflowStep(order=4, action="Implement minimal code"),
            WorkflowStep(order=5, action="Run the test and confirm GREEN"),
            WorkflowStep(order=6, action="Refactor if needed"),
            WorkflowStep(order=7, action="Run the broader test set"),
        ],
        exit_condition="all scoped implementation tasks are complete and the relevant test set is green",
    ),
    rules=[
        "Write a test that fails for the intended reason before writing implementation.",
        "Keep refactors behind green tests.",
    ],
    exceptions=[
        "Pure documentation, prompt-only edits, generated assets, or configuration-only changes.",
    ],
)


VERIFICATION_BEFORE_COMPLETION = WorkflowNode(
    name="verify",
    goal="Prove changes reach expected state with reproducible evidence",
    description="Use before claiming work is complete, fixed, passing, ready, or safe to merge.",
    persona="review",
    io=NodeIO(
        input={
            "claim": "Claimed completion status (done/fixed/passing)",
            "files_changed": "Changed files",
            "test_commands": "Related test commands",
        },
        output={
            "evidence": "Verification evidence including commands and output",
            "verified": "Whether verified",
            "scope": "Change impact scope (substantial/routine)",
        },
    ),
    gate=NodeGate(
        description="Before claiming any status, identify the proving command, run it in this turn, read the output, and report the evidence.",
        required_before_transition="verification command run with evidence",
    ),
    workflow=[
        WorkflowStep(order=1, action="Identify proving commands"),
        WorkflowStep(order=2, action="Run fresh verification"),
        WorkflowStep(order=3, action="Read output and classify result"),
        WorkflowStep(order=4, action="Report evidence"),
    ],
    rules=[
        "Evidence before completion claims.",
        "Do not rely on earlier runs or partial checks.",
    ],
)


REQUESTING_CODE_REVIEW = WorkflowNode(
    name="review",
    goal="Evaluate work and produce an evidence-based review verdict",
    description="Use when reviewing code, design, implementation, or changes for correctness, completeness, style, or risk.",
    persona="review",
    io=NodeIO(
        input={
            "files_changed": "Changed files",
            "verification_evidence": "Verification evidence",
            "risks": "Risk points",
        },
        output={
            "review_brief": "Review brief",
            "review_result": "Review result (PASS/FAIL)",
        },
    ),
    gate=NodeGate(
        description=(
            "Do not report a review verdict without checking the supplied requirements, "
            "changes, verification evidence, and risks."
        ),
        required_before_transition="review completed with an evidence-based PASS/FAIL verdict",
    ),
    workflow=[
        WorkflowStep(order=1, action="Define the review scope"),
        WorkflowStep(order=2, action="Perform the review and produce a verdict"),
    ],
    subworkflow=NodeSubworkflow(
        name="Review Cycle",
        description="Repeat until review verdict is resolved and either PASS is reached or feedback is routed.",
        steps=[
            WorkflowStep(order=1, action="Construct review brief"),
            WorkflowStep(order=2, action="Perform the review directly or request independent review when delegation is available"),
            WorkflowStep(order=3, action="Record evidence and verdict"),
            WorkflowStep(order=4, action="Route verdict"),
        ],
        exit_condition="review verdict is PASS, or feedback is handed off to feedback",
    ),
)


RECEIVING_CODE_REVIEW = WorkflowNode(
    name="feedback",
    goal="Verify and implement valid review feedback",
    description="Use when receiving review feedback, requested optimizations, or reviewer comments before implementing them.",
    persona="implement",
    io=NodeIO(
        input={
            "feedback": "Review feedback content",
            "source": "Feedback source (human/external)",
        },
        output={
            "changes_made": "Changes made based on feedback",
            "feedback_status": "Per-item feedback status (accepted/rejected/deferred)",
            "deferred_items": "Items needing design/analysis/planning rather than direct implementation",
        },
    ),
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
        WorkflowStep(order=6, action="Implement valid feedback", description="One coherent item at a time. Route items needing design to needs_design; items needing planning to needs_plan."),
        WorkflowStep(order=7, action="Verify", description="Run targeted tests or commands before reporting."),
    ],
    rules=[
        "Verify feedback against the codebase before changing code.",
        "Clarify unclear feedback before implementing the batch.",
        "If feedback asks for a proper feature, search for actual usage before expanding the code.",
        "If some feedback items need design or analysis rather than direct implementation, implement the actionable items first, then use needs_design to route the remaining items to brainstorm.",
        "If some feedback items have clear requirements but need a structured implementation plan, use needs_plan to route them to plan.",
    ],
)


SYSTEMATIC_DEBUGGING = WorkflowNode(
    name="debug",
    goal="Locate root cause and confirm fix direction",
    description="Use when debugging bugs, failed tests, build failures, tracebacks, crashes, or unexpected behavior.",
    persona="explore",
    io=NodeIO(
        input={
            "error": "Error message or abnormal behavior",
            "scenario": "Scenario and context where the problem occurs",
            "reproduction": "Reproduction steps",
        },
        output={
            "root_cause": "Root cause description",
            "fix_direction": "Fix direction description",
            "fix_type": "Fix type (trivial/nontrivial)",
        },
    ),
    gate=NodeGate(
        description="Root cause investigation must complete before proposing or applying any fix.",
        required_before_transition="root cause identified with evidence",
    ),
    workflow=[
        WorkflowStep(order=1, action="Run the internal debug cycle"),
        WorkflowStep(order=2, action="Classify fix type", description="Decide trivial_fix, nontrivial_fix, or done."),
    ],
    subworkflow=NodeSubworkflow(
        name="Debug Cycle",
        description="Repeat until root cause is confirmed and fix direction is known.",
        steps=[
            WorkflowStep(order=1, action="Clarify the problem scenario", description="Confirm what the user was doing, expected behavior, actual behavior, and environment before investigating."),
            WorkflowStep(order=2, action="Read the full error and reproduce consistently"),
            WorkflowStep(order=3, action="Find working examples and compare differences"),
            WorkflowStep(order=4, action="Form one concrete hypothesis"),
            WorkflowStep(order=5, action="Test the hypothesis minimally", description="Read-only: inspect logs, check config, compare outputs."),
        ],
        exit_condition="root cause confirmed and fix direction is known",
    ),
    rules=[
        "Find the root cause before changing code.",
        '"I will just try this fix" is guessing, not debugging.',
        "For flaky failures, gather observations and isolate variables before changing code.",
    ],
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
