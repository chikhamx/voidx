"""Default DAG for built-in workflow nodes."""

from __future__ import annotations

from voidx.workflow.nodes import BUILTIN_WORKFLOW_NODES
from voidx.workflow.schema import Edge, GoalEntry, WorkflowDAG


DEFAULT_WORKFLOW_DAG = WorkflowDAG(
    name="default",
    nodes=BUILTIN_WORKFLOW_NODES,
    edges=[
        Edge(source="brainstorm", target="design-doc", condition="approved", label="design approved"),
        Edge(source="brainstorm", target="plan", condition="skip_to_plan", label="user says to skip design or provides a detailed spec"),
        Edge(source="brainstorm", target="tdd", condition="small_change", label="small scoped change"),
        Edge(source="design-doc", target="plan", condition="completed", label="doc passes reader test"),
        Edge(source="plan", target="tdd", condition="approved", label="plan approved"),
        Edge(source="tdd", target="verify", condition="implemented", label="implementation complete"),
        Edge(source="verify", target="review", condition="passed_substantial", label="verification passed after substantial work"),
        Edge(source="verify", target="tdd", condition="failed_implementation", label="verification failed due to implementation issue"),
        Edge(source="verify", target="debug", condition="failed_bug", label="verification exposed a bug"),
        Edge(source="review", target="review-feedback", condition="review_has_issues", label="review returned issues"),
        Edge(source="review-feedback", target="tdd", condition="feedback_valid", label="feedback verified and valid"),
        Edge(source="review-feedback", target="verify", condition="feedback_verified", label="feedback implemented and needs verification"),
        Edge(source="debug", target="tdd", condition="nontrivial_fix", label="fix requires TDD"),
        Edge(source="debug", target="verify", condition="trivial_fix", label="fix is trivial"),
    ],
    goal_map=[
        GoalEntry(goal_type="debug", nodes=["debug"], reason="goal:debug"),
        GoalEntry(goal_type="bugfix", nodes=["debug", "tdd", "verify"], reason="goal:bugfix"),
        GoalEntry(goal_type="feature", nodes=["brainstorm"], reason="goal:feature"),
        GoalEntry(goal_type="refactor", nodes=["brainstorm", "plan"], reason="goal:refactor"),
        GoalEntry(goal_type="design", nodes=["brainstorm"], reason="goal:design"),
        GoalEntry(goal_type="doc", nodes=["design-doc"], reason="goal:doc"),
        GoalEntry(goal_type="review", nodes=["review"], reason="goal:review"),
        GoalEntry(goal_type="chore", nodes=["tdd", "verify"], reason="goal:chore"),
    ],
)
