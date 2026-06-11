"""Workflow activation and gate policy."""

from __future__ import annotations

from dataclasses import dataclass

from voidx.workflow.dag import DEFAULT_WORKFLOW_DAG
from voidx.workflow.schema import Edge, NodeGate


@dataclass(frozen=True)
class WorkflowActivation:
    name: str
    reason: str


WORKFLOW_PRIORITY = {
    name: node.priority
    for name, node in DEFAULT_WORKFLOW_DAG.nodes.items()
}

WORKFLOW_TRANSITIONS: dict[str, tuple[str, ...]] = {
    name: tuple(edge.target for edge in DEFAULT_WORKFLOW_DAG.edges_from(name))
    for name in DEFAULT_WORKFLOW_DAG.nodes
}


def workflow_activations(
    user_text: str,
    *,
    agent: str = "",
    task_intent: str | None = None,
    interaction_mode: str | None = None,
) -> list[WorkflowActivation]:
    text = user_text.strip().lower()
    agent_name = (agent or "").strip().lower()
    intent = (task_intent or "").strip().lower()
    mode = (interaction_mode or "").strip().lower()
    activations: dict[str, WorkflowActivation] = {}

    def add(name: str, reason: str) -> None:
        activations.setdefault(name, WorkflowActivation(name=name, reason=reason))

    if intent == "debug":
        add("systematic-debugging", "debug intent")
        add("test-driven-development", "debug fix lifecycle")
        add("verification-before-completion", "debug lifecycle")

    if agent_name == "implement":
        add("test-driven-development", "implement role")
        add("verification-before-completion", "implement lifecycle")
    elif intent == "implement":
        add("test-driven-development", "implement intent")
        add("verification-before-completion", "implement lifecycle")

    if agent_name == "plan":
        add("writing-plans", "plan role")

    if intent == "review":
        if _contains_any(text, _REVIEW_FEEDBACK_TERMS):
            add("receiving-code-review", "review feedback")
        else:
            add("requesting-code-review", "review intent")

    if intent == "design":
        add("brainstorming", "design intent")
        if _contains_any(text, _PLAN_TERMS):
            add("writing-plans", "planning intent")

    if mode == "plan":
        add("brainstorming", "plan mode")
        add("writing-plans", "plan mode")

    return sorted(
        activations.values(),
        key=lambda item: (WORKFLOW_PRIORITY.get(item.name, 999), item.name),
    )


def workflow_sort_key(name: str) -> tuple[int, str]:
    return (WORKFLOW_PRIORITY.get(name, 999), name)


def workflow_transitions(name: str) -> tuple[str, ...]:
    return WORKFLOW_TRANSITIONS.get(name.strip().lower(), ())


def workflow_edges(name: str) -> tuple[Edge, ...]:
    return tuple(DEFAULT_WORKFLOW_DAG.edges_from(name))


def workflow_gate(name: str) -> NodeGate | None:
    return DEFAULT_WORKFLOW_DAG.gate_for(name)


def workflow_denied_tools(active_names: list[str]) -> set[str]:
    return DEFAULT_WORKFLOW_DAG.all_denied_tools(active_names)


def workflow_exit_summaries(name: str) -> list[str]:
    summaries = [
        f"{edge.condition} -> {edge.target}"
        + (f" ({edge.label})" if edge.label else "")
        for edge in DEFAULT_WORKFLOW_DAG.edges_from(name)
    ]
    if summaries:
        summaries.append(DEFAULT_WORKFLOW_DAG.terminal_exit_summary())
    return summaries


def workflow_terminal_condition() -> str:
    return DEFAULT_WORKFLOW_DAG.terminal_exit.condition


def workflow_terminal_description() -> str:
    return DEFAULT_WORKFLOW_DAG.terminal_exit.description


def is_workflow_terminal_condition(condition: str) -> bool:
    return DEFAULT_WORKFLOW_DAG.is_terminal_condition(condition)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


_REVIEW_FEEDBACK_TERMS = (
    "review feedback",
    "code review feedback",
    "review comment",
    "reviewer says",
    "feedback says",
    "优化点",
    "审查意见",
    "评审意见",
)

_PLAN_TERMS = (
    "implementation plan",
    "write a plan",
    "planning",
    "spec",
    "requirements",
    "计划",
    "实施方案",
    "需求",
)
