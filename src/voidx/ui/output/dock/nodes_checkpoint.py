"""Checkpoint prompt node mutations for BottomInputDock."""

from __future__ import annotations

from typing import Any

from rich.markup import escape

from voidx.ui.output.tree import OutputNode


class DockCheckpointNodeMixin:
    def show_checkpoint(
        self,
        checkpoint_id: str,
        plan: dict[str, Any],
        choices: list[dict[str, Any]],
        *,
        parent: OutputNode | None = None,
    ) -> OutputNode:
        body = _checkpoint_body(plan, choices)
        node = self._tree.new_node(
            parent=parent or self.ensure_agent(),
            node_type="checkpoint",
            header="[yellow]●[/yellow] [bold]voidx plan[/bold]",
            body_lines=body,
            collapsed=False,
            payload={
                "interaction": "checkpoint",
                "checkpoint_id": checkpoint_id,
                "plan": plan,
                "choices": choices,
            },
        )
        self._checkpoint_nodes[checkpoint_id] = node
        self._mark_unsettled(node)
        self.refresh()
        return node

    def resolve_checkpoint(
        self,
        checkpoint_id: str,
        decision: str,
        label: str,
        response: str,
        *,
        was_custom_input: bool = False,
    ) -> None:
        node = self._checkpoint_nodes.get(checkpoint_id)
        if node is None:
            return
        display_response = response or label or decision
        color = "red" if decision == "rejected" else "dim"
        node.header = f"[{color}]●[/{color}] [{color}]voidx plan {escape(decision)}[/{color}]"
        node.status = "done"
        node.payload["decision"] = decision
        node.payload["response"] = display_response
        node.payload["was_custom_input"] = was_custom_input
        child = self._tree.new_node(
            parent=node,
            node_type="message",
            header=f"[white on #3a3937]User: {escape(display_response)}[/]",
            collapsed=False,
        )
        self._mark_subtree_settled(child)
        self._mark_subtree_settled(node)
        self._tree.mark_dirty()
        self.refresh()


def _checkpoint_body(plan: dict[str, Any], choices: list[dict[str, Any]]) -> list[str]:
    body: list[str] = []
    summary = str(plan.get("plan_summary") or "").strip()
    if summary:
        body.append(escape(f"Plan: {summary}"))
    steps = _string_list(plan.get("steps"))
    if steps:
        if body:
            body.append("")
        body.append("[bold]Steps:[/bold]")
        body.extend(escape(f"{index}. {step}") for index, step in enumerate(steps, 1))
    affected_files = _string_list(plan.get("affected_files"))
    if affected_files:
        if body:
            body.append("")
        body.append("[bold]Affected files:[/bold]")
        body.extend(escape(path) for path in affected_files)
    risks = _string_list(plan.get("risks"))
    if risks:
        if body:
            body.append("")
        body.append("[bold]Risks:[/bold]")
        body.extend(escape(f"- {risk}") for risk in risks)
    if choices:
        if body:
            body.append("")
        body.append("[bold]Choices:[/bold]")
        for choice in choices:
            label = str(choice.get("label") or choice.get("value") or "").strip()
            description = str(choice.get("description") or "").strip()
            if description:
                body.append(escape(f"- {label}: {description}"))
            elif label:
                body.append(escape(f"- {label}"))
    return body


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]
