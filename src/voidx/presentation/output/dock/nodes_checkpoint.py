"""Checkpoint prompt node mutations for BottomInputDock."""

from __future__ import annotations

from typing import Any

from rich.markup import escape

from voidx.logging.tool_log import log_tool_event
from voidx.presentation.output.tree import OutputNode


class DockCheckpointNodeMixin:
    def show_checkpoint(
        self,
        checkpoint_id: str,
        plan: dict[str, Any],
        choices: list[dict[str, Any]],
        *,
        parent: OutputNode | None = None,
    ) -> OutputNode:
        body = _checkpoint_body(plan)
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
        self._mark_subtree_settled(node)
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
            log_tool_event("ui_checkpoint_orphan", tool_name="dock", message=f"Checkpoint decision received for unknown checkpoint_id={checkpoint_id}")
            return
        display_response = response or label or decision
        color = _decision_color(decision)
        node.header = f"[{color}]●[/{color}] [{color}]voidx plan {escape(decision)}[/{color}]"
        node.status = "done"
        node.payload["decision"] = decision
        node.payload["response"] = display_response
        node.payload["was_custom_input"] = was_custom_input
        child = self._tree.new_node(
            parent=node,
            node_type="message",
            header=f"{_DECISION_LABEL} {escape(display_response)}",
            collapsed=False,
            payload={"full_width_user_row": True, "align_full_width_user_row": True},
        )
        self._mark_subtree_settled(child)
        self._mark_subtree_settled(node)
        self._tree.mark_dirty()
        self.refresh()


_PLAN_LABEL = "[#EBCB8B]Plan:[/#EBCB8B]"
_DECISION_LABEL = "[#EBCB8B]Decision:[/#EBCB8B]"
_SECTION_TITLE = "[bold #D8DEE9]{}:[/bold #D8DEE9]"
_BODY = "[#D8DEE9]{}[/#D8DEE9]"
_STEP_NUM = "[#61AFEF]{}.[/#61AFEF]"
_PATH = "[#56D4DD]{}[/#56D4DD]"
_RISK_PREFIX = "[#E06C75]-[/#E06C75]"
_RISK_BODY = "[#E06C75]{}[/#E06C75]"


def _checkpoint_body(plan: dict[str, Any]) -> list[str]:
    body: list[str] = []
    summary = str(plan.get("goal") or "").strip()
    if summary:
        body.append(f"{_PLAN_LABEL} {_BODY.format(escape(summary))}")
    steps = _string_list(plan.get("steps"))
    if steps:
        if body:
            body.append("")
        body.append(_SECTION_TITLE.format("Steps"))
        for index, step in enumerate(steps, 1):
            body.append(f"{_STEP_NUM.format(index)} {_BODY.format(escape(step))}")
    affected_files = _string_list(plan.get("affected_files"))
    if affected_files:
        if body:
            body.append("")
        body.append(_SECTION_TITLE.format("Affected files"))
        body.extend(_PATH.format(escape(path)) for path in affected_files)
    risks = _string_list(plan.get("risks"))
    if risks:
        if body:
            body.append("")
        body.append(_SECTION_TITLE.format("Risks"))
        body.extend(f"{_RISK_PREFIX} {_RISK_BODY.format(escape(risk))}" for risk in risks)
    return body


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _decision_color(decision: str) -> str:
    if decision == "rejected":
        return "red"
    if decision == "needs_doc":
        return "yellow"
    if decision == "modified":
        return "cyan"
    return "dim"
