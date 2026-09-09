"""Loop spec approval prompt node mutations for BottomInputDock."""

from __future__ import annotations

from typing import Any

from rich.markup import escape

from voidx.observability.tool_log import log_tool_event
from voidx.presentation.output.tree import OutputNode


class DockLoopSpecNodeMixin:
    def show_loop_spec(
        self,
        prompt_id: str,
        spec: dict[str, Any],
        choices: list[dict[str, Any]],
        *,
        parent: OutputNode | None = None,
    ) -> OutputNode:
        node = self._tree.new_node(
            parent=parent or self.ensure_agent(),
            node_type="loop_spec",
            header="[yellow]●[/yellow] [bold]loop spec[/bold]",
            body_lines=_loop_spec_body(spec),
            collapsed=False,
            payload={
                "interaction": "loop_spec",
                "prompt_id": prompt_id,
                "spec": spec,
                "choices": choices,
            },
        )
        self._loop_spec_nodes[prompt_id] = node
        self._mark_unsettled(node)
        self.refresh()
        return node

    def resolve_loop_spec(
        self,
        prompt_id: str,
        decision: str,
        response: str,
    ) -> None:
        node = self._loop_spec_nodes.get(prompt_id)
        if node is None:
            log_tool_event("ui_loop_spec_orphan", tool_name="dock", message=f"Loop spec decision received for unknown prompt_id={prompt_id}")
            return
        label = _DECISION_LABELS.get(decision, decision)
        color = _decision_color(decision)
        node.header = f"[{color}]●[/{color}] [{color}]loop spec {escape(label)}[/{color}]"
        node.status = "done"
        node.payload["decision"] = decision
        if not node.body_lines or node.body_lines[-1] != "":
            node.body_lines.append("")
        child_text = response or label
        child = self._tree.new_node(
            parent=node,
            node_type="message",
            header=f"{_DECISION_LABEL} {escape(child_text)}",
            collapsed=False,
            payload={"full_width_user_row": True, "align_full_width_user_row": True},
        )
        self._mark_completed(child)
        self._mark_completed(node)
        self._tree.mark_dirty()
        self.refresh()


_SPEC_LABEL = "[#EBCB8B]Loop:[/#EBCB8B]"
_DECISION_LABEL = "[#EBCB8B]Decision:[/#EBCB8B]"
_SECTION_TITLE = "[bold #D8DEE9]{}:[/bold #D8DEE9]"
_BODY = "[#D8DEE9]{}[/#D8DEE9]"

_DECISION_LABELS = {
    "approved": "approved",
    "auto_approved": "auto-approved",
    "revised": "revise requested",
    "cancelled": "cancelled",
}


def _loop_spec_body(spec: dict[str, Any]) -> list[str]:
    body: list[str] = []
    prompt = str(spec.get("prompt") or "").strip()
    if prompt:
        body.append(f"{_SPEC_LABEL} {_BODY.format(escape(prompt))}")
    interval = spec.get("interval_seconds")
    if interval is not None:
        interval_str = f"{interval:g}s (fixed)" if isinstance(interval, (int, float)) else f"{interval} (fixed)"
    else:
        interval_str = "dynamic"
    body.append(f"{_SECTION_TITLE.format('Interval')} {_BODY.format(escape(interval_str))}")
    return body


def _decision_color(decision: str) -> str:
    if decision in {"approved", "auto_approved"}:
        return "green"
    if decision == "cancelled":
        return "red"
    return "yellow"
