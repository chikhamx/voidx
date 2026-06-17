"""Default DAG for built-in workflow nodes."""

from __future__ import annotations

from voidx.workflow.nodes import BUILTIN_WORKFLOW_NODES
from voidx.workflow.schema import Edge, GoalEntry, WorkflowDAG


DEFAULT_WORKFLOW_DAG = WorkflowDAG(
    name="default",
    nodes=BUILTIN_WORKFLOW_NODES,
    edges=[
        Edge(source="brainstorm", target="design", condition="approved", label="design approved", description="Use after explicit user approval when a design document is needed."),
        Edge(source="brainstorm", target="plan", condition="skip_to_plan", label="user says to skip design or provides a detailed spec", description="Use when the request is already a detailed spec or the user explicitly asks to skip the design document."),
        Edge(source="brainstorm", target="tdd", condition="small_change", label="small scoped change", description="Use for local or mechanical changes that do not need a plan."),
        Edge(source="design", target="plan", condition="completed", label="doc passes reader test", description="Use after the document passes reader test and accuracy verification."),
        Edge(source="plan", target="tdd", condition="approved", label="plan approved", description="Use after the implementation plan is executable and user-approved."),
        Edge(source="tdd", target="verify", condition="implemented", label="implementation complete", description="Use after implementation and relevant tests are green."),
        Edge(source="verify", target="tdd", condition="failed_implementation", label="verification failed due to implementation issue", description="Use when verification points to implementation work."),
        Edge(source="verify", target="debug", condition="failed_bug", label="verification exposed a bug", description="Use when verification exposes a bug or unclear root cause."),
        Edge(source="review", target="feedback", condition="review_has_issues", label="review returned issues", description="Use when the review verdict includes required changes."),
        Edge(source="feedback", target="tdd", condition="feedback_valid", label="feedback verified and valid", description="Use when valid feedback requires implementation changes."),
        Edge(source="feedback", target="verify", condition="feedback_verified", label="feedback implemented and needs verification", description="Use after feedback has been implemented and needs verification."),
        Edge(source="feedback", target="brainstorm", condition="needs_design", label="feedback requires design or analysis", description="Use when some feedback items need design exploration or impact analysis rather than direct implementation."),
        Edge(source="feedback", target="plan", condition="needs_plan", label="feedback requires implementation planning", description="Use when some feedback items have clear requirements but need a structured implementation plan before coding."),
        Edge(source="debug", target="tdd", condition="nontrivial_fix", label="fix requires TDD", description="Use when the fix requires a nontrivial implementation change."),
        Edge(source="debug", target="verify", condition="trivial_fix", label="fix is trivial", description="Use when the fix is small enough to verify directly."),
        Edge(source="verify", target="review", condition="passed_substantial", label="verification passed with substantial changes", description="Use when verification passes and the change is substantial enough to warrant review."),
    ],
    goal_map=[
        GoalEntry(goal_type="debug", nodes=["debug"], reason="goal:debug"),
        GoalEntry(goal_type="bugfix", nodes=["debug", "tdd", "verify"], reason="goal:bugfix"),
        GoalEntry(goal_type="feature", nodes=["brainstorm"], reason="goal:feature"),
        GoalEntry(goal_type="refactor", nodes=["brainstorm"], reason="goal:refactor"),
        GoalEntry(goal_type="design", nodes=["brainstorm"], reason="goal:design"),
        GoalEntry(goal_type="doc", nodes=["design"], reason="goal:doc"),
        GoalEntry(goal_type="review", nodes=["review"], reason="goal:review"),
        GoalEntry(goal_type="chore", nodes=["tdd", "verify"], reason="goal:chore"),
        GoalEntry(goal_type="inspect", nodes=["brainstorm"], reason="goal:inspect"),
    ],
)
