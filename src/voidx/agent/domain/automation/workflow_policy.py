"""Workflow gate policy computed from an explicit DAG."""

from __future__ import annotations

from voidx.agent.domain.automation.workflow_schema import Edge, NodeGate, WorkflowDAG


_NODE_PRIORITY = {
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


def workflow_sort_key(name: str, dag: WorkflowDAG) -> tuple[int, str]:
    del dag
    return (_NODE_PRIORITY.get(name, 999), name)


def workflow_transitions(name: str, dag: WorkflowDAG) -> tuple[str, ...]:
    return tuple(edge.target for edge in dag.edges_from(name))


def workflow_edges(name: str, dag: WorkflowDAG) -> tuple[Edge, ...]:
    return tuple(dag.edges_from(name))


def workflow_gate(name: str, dag: WorkflowDAG) -> NodeGate | None:
    return dag.gate_for(name)


def workflow_personas(name: str, dag: WorkflowDAG) -> tuple[str, ...]:
    node = dag.nodes.get(name.strip().lower())
    return (node.persona,) if node else ()


def workflow_exit_summaries(name: str, dag: WorkflowDAG) -> list[str]:
    summaries = [
        f"{edge.condition} -> {edge.target}"
        + (f" ({edge.label})" if edge.label else "")
        for edge in dag.edges_from(name)
    ]
    if summaries:
        summaries.append(dag.terminal_exit_summary())
    return summaries


def workflow_terminal_condition(dag: WorkflowDAG) -> str:
    return dag.terminal_exit.condition


def workflow_terminal_description(dag: WorkflowDAG) -> str:
    return dag.terminal_exit.description


def is_workflow_terminal_condition(condition: str, dag: WorkflowDAG) -> bool:
    return dag.is_terminal_condition(condition)
