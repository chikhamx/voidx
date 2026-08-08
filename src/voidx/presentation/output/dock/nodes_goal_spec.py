"""Goal spec approval prompt node mutations for BottomInputDock."""

from __future__ import annotations

from typing import Any

from rich.markup import escape

from voidx.observability.tool_log import log_tool_event
from voidx.presentation.output.tree import OutputNode


class DockGoalSpecNodeMixin:
    def show_goal_spec(
        self,
        prompt_id: str,
        spec: dict[str, Any],
        choices: list[dict[str, Any]],
        *,
        parent: OutputNode | None = None,
    ) -> OutputNode:
        node = self._tree.new_node(
            parent=parent or self.ensure_agent(),
            node_type="goal_spec",
            header="[yellow]●[/yellow] [bold]goal spec[/bold]",
            body_lines=_goal_spec_body(spec),
            collapsed=False,
            payload={
                "interaction": "goal_spec",
                "prompt_id": prompt_id,
                "spec": spec,
                "choices": choices,
            },
        )
        self._goal_spec_nodes[prompt_id] = node
        self._mark_subtree_settled(node)
        self.refresh()
        return node

    def resolve_goal_spec(
        self,
        prompt_id: str,
        decision: str,
        response: str,
    ) -> None:
        node = self._goal_spec_nodes.get(prompt_id)
        if node is None:
            log_tool_event("ui_goal_spec_orphan", tool_name="dock", message=f"Goal spec decision received for unknown prompt_id={prompt_id}")
            return
        label = _DECISION_LABELS.get(decision, decision)
        color = _decision_color(decision)
        node.header = f"[{color}]●[/{color}] [{color}]goal spec {escape(label)}[/{color}]"
        node.status = "done"
        node.payload["decision"] = decision
        child_text = response or label
        child = self._tree.new_node(
            parent=node,
            node_type="message",
            header=f"{_DECISION_LABEL} {escape(child_text)}",
            collapsed=False,
            payload={"full_width_user_row": True, "align_full_width_user_row": True},
        )
        self._mark_subtree_settled(child)
        self._mark_subtree_settled(node)
        self._tree.mark_dirty()
        self.refresh()


_SPEC_LABEL = "[#EBCB8B]Goal:[/#EBCB8B]"
_DECISION_LABEL = "[#EBCB8B]Decision:[/#EBCB8B]"
_SECTION_TITLE = "[bold #D8DEE9]{}:[/bold #D8DEE9]"
_BODY = "[#D8DEE9]{}[/#D8DEE9]"

_DECISION_LABELS = {
    "approved": "approved",
    "auto_approved": "auto-approved",
    "revised": "revise requested",
    "cancelled": "cancelled",
}


def _goal_spec_body(spec: dict[str, Any]) -> list[str]:
    body: list[str] = []
    objective = str(spec.get("objective") or "").strip()
    if objective:
        body.append(f"{_SPEC_LABEL} {_BODY.format(escape(objective))}")
    acceptance = str(spec.get("acceptance_condition") or "").strip()
    if acceptance:
        if body:
            body.append("")
        body.append(_SECTION_TITLE.format("Acceptance"))
        body.append(_BODY.format(escape(acceptance)))
    method = str(spec.get("achievement_method") or "").strip()
    if method:
        if body:
            body.append("")
        body.append(_SECTION_TITLE.format("Method"))
        body.append(_BODY.format(escape(method)))
    attempts = spec.get("max_attempts")
    if attempts:
        if body:
            body.append("")
        body.append(_SECTION_TITLE.format("Attempt budget"))
        body.append(_BODY.format(str(attempts)))
    return body


def _decision_color(decision: str) -> str:
    if decision in {"approved", "auto_approved"}:
        return "green"
    if decision == "cancelled":
        return "red"
    return "yellow"
