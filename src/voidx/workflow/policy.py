"""Workflow gate policy."""

from __future__ import annotations

from voidx.workflow.dag import DEFAULT_WORKFLOW_DAG
from voidx.workflow.schema import Edge, NodeGate


WORKFLOW_PRIORITY = {
    name: index
    for index, name in enumerate((
        "debug",
        "feedback",
        "brainstorm",
        "design",
        "plan",
        "tdd",
        "verify",
        "review",
    ))
}

WORKFLOW_TRANSITIONS: dict[str, tuple[str, ...]] = {
    name: tuple(edge.target for edge in DEFAULT_WORKFLOW_DAG.edges_from(name))
    for name in DEFAULT_WORKFLOW_DAG.nodes
}


def workflow_sort_key(name: str) -> tuple[int, str]:
    return (WORKFLOW_PRIORITY.get(name, 999), name)


def workflow_transitions(name: str) -> tuple[str, ...]:
    return WORKFLOW_TRANSITIONS.get(name.strip().lower(), ())


def workflow_edges(name: str) -> tuple[Edge, ...]:
    return tuple(DEFAULT_WORKFLOW_DAG.edges_from(name))


def workflow_gate(name: str) -> NodeGate | None:
    return DEFAULT_WORKFLOW_DAG.gate_for(name)



def workflow_personas(name: str) -> tuple[str, ...]:
    node = DEFAULT_WORKFLOW_DAG.nodes.get(name.strip().lower())
    return (node.persona,) if node else ()


def workflow_denied_tools(active_names: list[str]) -> set[str]:
    if not active_names:
        return set()
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

