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
        f"Priority: {node.priority}",
    ]
    if node.triggers:
        lines.append(f"Triggers: {', '.join(node.triggers)}")
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
        f"Priority: {node.priority}",
    ]
    if node.triggers:
        lines.append(f"Triggers: {', '.join(node.triggers)}")
    if node.core_rule:
        lines.extend(["", "### Core Rule", node.core_rule])
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
    if node.decision_rules:
        lines.extend(["", "### Decision Rules"])
        for rule in node.decision_rules:
            lines.append(f"- `{rule.condition}`: {rule.description}")
    if dag:
        edges = dag.edges_from(node.name)
        if edges:
            lines.extend(["", "### Available Exits"])
            for edge in edges:
                label = f" ({edge.label})" if edge.label else ""
                lines.append(f"- `{edge.condition}` -> `{edge.target}`{label}")
            terminal = dag.terminal_exit
            lines.append(f"- `{terminal.condition}` -> {terminal.description}")
    if node.allowed_exceptions:
        lines.extend(["", "### Allowed Exceptions"])
        lines.extend(f"- {item}" for item in node.allowed_exceptions)
    if node.anti_patterns:
        lines.extend(["", "### Anti-Patterns"])
        lines.extend(f"- {item}" for item in node.anti_patterns)
    for title, body in node.extra_sections.items():
        lines.extend(["", f"### {title}", body.strip()])
    return "\n".join(lines).strip() + "\n"
