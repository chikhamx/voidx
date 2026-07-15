"""Rendering helpers for structured workflow context."""

from __future__ import annotations

from voidx.workflow.schema import WorkflowDAG, WorkflowNode


def render_dag_overview(dag: WorkflowDAG) -> str:
    lines: list[str] = [
        f"Name: {dag.name}",
        "Workflow node definitions live in the separate Workflow Context message.",
        "Use Current Task State to determine which nodes are active for this turn.",
        "",
        "Entry goal types:",
    ]
    if dag.goal_map:
        for entry in sorted(dag.goal_map, key=lambda item: item.goal_type):
            reason = f" ({entry.reason})" if entry.reason else ""
            lines.append(f"- {entry.goal_type}: {', '.join(entry.nodes)}{reason}")
    else:
        lines.append("- none")

    lines.extend(["", "Edges:"])
    if dag.edges:
        for edge in sorted(dag.edges, key=lambda item: (item.source, item.condition, item.target)):
            label = f" ({edge.label})" if edge.label else ""
            lines.append(f"- {edge.source} --{edge.condition}--> {edge.target}{label}")
    else:
        lines.append("- none")
    terminal = dag.terminal_exit
    lines.append(f"- {terminal.condition}: {terminal.description}")
    return "\n".join(lines).strip()


def render_node_summary(node: WorkflowNode, dag: WorkflowDAG | None = None) -> str:
    lines: list[str] = [
        f"## Workflow Node Summary: {node.name}",
        f"Description: {node.description}",
        f"Goal: {node.goal}",
    ]
    if dag:
        exits = dag.edges_from(node.name)
        if exits:
            formatted = [
                f"{edge.condition} -> {edge.target}"
                + (f" ({edge.label})" if edge.label else "")
                for edge in exits
            ]
            formatted.append(dag.terminal_exit_summary())
            lines.append(f"Exits: {'; '.join(formatted)}")
    return "\n".join(lines).strip() + "\n"


def render_node_markdown(node: WorkflowNode, dag: WorkflowDAG | None = None) -> str:
    lines: list[str] = [
        f"## Workflow Node: {node.name}",
        f"Description: {node.description}",
    ]
    if node.goal:
        lines.extend(["", "### Goal", node.goal])
    if node.gate.description or node.gate.required_before_transition:
        lines.extend(["", "### Gate"])
        if node.gate.required_before_transition:
            lines.append(f"Required before transition: {node.gate.required_before_transition}")
        if node.gate.description:
            lines.append(node.gate.description)
    if node.workflow:
        lines.extend(["", "### Workflow"])
        for step in sorted(node.workflow, key=lambda item: item.order):
            suffix = f": {step.description}" if step.description else ""
            lines.append(f"{step.order}. {step.action}{suffix}")
    if node.subworkflow:
        sub = node.subworkflow
        lines.extend(["", f"### Internal Subworkflow: {sub.name}"])
        if sub.description:
            lines.append(f"Description: {sub.description}")
        for step in sorted(sub.steps, key=lambda item: item.order):
            suffix = f": {step.description}" if step.description else ""
            lines.append(f"{step.order}. {step.action}{suffix}")
        lines.append(f"Exit condition: {sub.exit_condition}")
    if node.rules:
        lines.extend(["", "### Rules"])
        lines.extend(f"- {item}" for item in node.rules)
    if node.exceptions:
        lines.extend(["", "### Exceptions"])
        lines.extend(f"- {item}" for item in node.exceptions)
    return "\n".join(lines).strip() + "\n"
