"""Clarify prompt node mutations for BottomInputDock."""

from __future__ import annotations

import logging

from rich.markup import escape

from voidx.ui.output.tree import OutputNode


logger = logging.getLogger(__name__)


class DockClarifyNodeMixin:
    def show_clarify(
        self,
        clarify_id: str,
        question: str,
        options: list[str],
        *,
        parent: OutputNode | None = None,
    ) -> OutputNode:
        body = _clarify_body(question, options)
        node = self._tree.new_node(
            parent=parent or self.ensure_agent(),
            node_type="clarify",
            header="[yellow]●[/yellow] [bold]voidx clarify[/bold]",
            body_lines=body,
            collapsed=False,
            payload={
                "interaction": "clarify",
                "clarify_id": clarify_id,
                "question": question,
                "options": options,
            },
        )
        self._clarify_nodes[clarify_id] = node
        self._mark_subtree_settled(node)
        self.refresh()
        return node

    def resolve_clarify(
        self,
        clarify_id: str,
        answer: str,
        *,
        cancelled: bool = False,
        was_custom_input: bool = True,
    ) -> None:
        node = self._clarify_nodes.get(clarify_id)
        if node is None:
            logger.debug("Clarify answer received for unknown clarify_id=%s", clarify_id)
            return
        display_response = answer or ("skipped" if cancelled else "")
        color = "red" if cancelled else "cyan"
        state = "skipped" if cancelled else "answered"
        node.header = f"[{color}]●[/{color}] [{color}]voidx clarify {escape(state)}[/{color}]"
        node.status = "done"
        node.payload["answer"] = answer
        node.payload["cancelled"] = cancelled
        node.payload["was_custom_input"] = was_custom_input
        child = self._tree.new_node(
            parent=node,
            node_type="message",
            header=f"[white on #3a3937]User: {escape(display_response)}[/]",
            collapsed=False,
            payload={"full_width_user_row": True},
        )
        self._mark_subtree_settled(child)
        self._mark_subtree_settled(node)
        self._tree.mark_dirty()
        self.refresh()


_QUESTION_LABEL = "[#EBCB8B]Question:[/#EBCB8B]"
_SECTION_TITLE = "[bold #D8DEE9]{}:[/bold #D8DEE9]"
_BODY = "[#D8DEE9]{}[/#D8DEE9]"
_SUGGESTION_PREFIX = "[#61AFEF]-[/#61AFEF]"


def _clarify_body(question: str, options: list[str]) -> list[str]:
    body: list[str] = []
    if question.strip():
        body.append(f"{_QUESTION_LABEL} {_BODY.format(escape(question))}")
    suggestions = [str(o) for o in options if str(o).strip()]
    if suggestions:
        if body:
            body.append("")
        body.append(_SECTION_TITLE.format("Suggestions"))
        body.extend(f"{_SUGGESTION_PREFIX} {_BODY.format(escape(s))}" for s in suggestions)
    return body
