"""Default DAG for built-in workflow nodes."""

from __future__ import annotations

from voidx.workflow.nodes import BUILTIN_WORKFLOW_NODES
from voidx.workflow.schema import Edge, IntentEntry, WorkflowDAG


DEFAULT_WORKFLOW_DAG = WorkflowDAG(
    name="default",
    nodes=BUILTIN_WORKFLOW_NODES,
    edges=[
        Edge(source="brainstorming", target="writing-design-docs", condition="approved", label="design approved"),
        Edge(source="brainstorming", target="writing-plans", condition="skip_to_plan", label="user says to skip design or provides a detailed spec"),
        Edge(source="brainstorming", target="test-driven-development", condition="small_change", label="small scoped change"),
        Edge(source="writing-design-docs", target="writing-plans", condition="completed", label="doc passes reader test"),
        Edge(source="writing-plans", target="test-driven-development", condition="approved", label="plan approved"),
        Edge(source="test-driven-development", target="verification-before-completion", condition="implemented", label="implementation complete"),
        Edge(source="verification-before-completion", target="requesting-code-review", condition="passed_substantial", label="verification passed after substantial work"),
        Edge(source="verification-before-completion", target="test-driven-development", condition="failed_implementation", label="verification failed due to implementation issue"),
        Edge(source="verification-before-completion", target="systematic-debugging", condition="failed_bug", label="verification exposed a bug"),
        Edge(source="requesting-code-review", target="receiving-code-review", condition="review_has_issues", label="review returned issues"),
        Edge(source="receiving-code-review", target="test-driven-development", condition="feedback_valid", label="feedback verified and valid"),
        Edge(source="receiving-code-review", target="verification-before-completion", condition="feedback_verified", label="feedback implemented and needs verification"),
        Edge(source="systematic-debugging", target="test-driven-development", condition="nontrivial_fix", label="fix requires TDD"),
        Edge(source="systematic-debugging", target="verification-before-completion", condition="trivial_fix", label="fix is trivial"),
    ],
    intent_map=[
        IntentEntry(intent="debug", nodes=["systematic-debugging", "test-driven-development", "verification-before-completion"], reason="debug intent"),
        IntentEntry(intent="implement", nodes=["test-driven-development", "verification-before-completion"], reason="implement intent"),
        IntentEntry(intent="design", nodes=["brainstorming"], reason="design intent"),
        IntentEntry(intent="review", nodes=["requesting-code-review"], reason="review intent"),
    ],
)
