"""Streaming output node management for BottomInputDock."""

from __future__ import annotations

from rich.markup import escape
from rich.console import Console
from rich.text import Text

from voidx.ui.output.dock.agent_placeholder import is_agent_placeholder_header
from voidx.ui.output.dock.formatting import _ansi_line, _ansi_rgb, _clean, _markdown_lines
from voidx.ui.output.tree import OutputNode


class DockStreamMixin:
    def set_stream(
        self,
        text: str,
        *,
        parent: OutputNode | None = None,
        phase: str = "text",
        refresh: bool = True,
    ) -> bool:
        if not self._active:
            return False
        self._stream_text = text
        self._update_stream_node(parent=parent, phase=phase)
        self._mark_unsettled(self._stream_node)
        if refresh:
            self.refresh()
        return True

    def commit_stream(self, *, refresh: bool = True) -> bool:
        if not self._active:
            return False
        if self._ignored_duplicate_stream_commit:
            self._ignored_duplicate_stream_commit = False
            self._stream_text = ""
            if refresh:
                self.refresh()
            return True
        stream_node = self._stream_node
        if stream_node is not None and stream_node is not self._current_agent:
            self._mark_settled(stream_node)
            self._last_committed_stream_text = _stream_signature(self._stream_text)
            self._last_committed_stream_parent_id = stream_node.parent.id if stream_node.parent else None
            self._last_committed_stream_node_id = stream_node.id
        self._stream_node = None
        self._stream_text = ""
        if refresh:
            self.refresh()
        return True

    def discard_stream(self) -> bool:
        if not self._active:
            return False
        if self._stream_node:
            self._remove_node(self._stream_node)
            if self._current_agent is self._stream_node:
                self._current_agent = None
        self._stream_node = None
        self._stream_text = ""
        self._last_committed_stream_text = ""
        self._last_committed_stream_parent_id = None
        self._last_committed_stream_node_id = None
        self._ignored_duplicate_stream_commit = False
        self.refresh()
        return True

    def _update_stream_node(
        self,
        *,
        parent: OutputNode | None = None,
        phase: str = "text",
    ) -> None:
        clean = _clean(self._stream_text).strip("\n")
        if not clean:
            return
        target = parent or self.ensure_agent()
        if phase != "thinking" and self._is_duplicate_committed_stream(clean, target):
            self._ignored_duplicate_stream_commit = True
            return
        stream_existed = self._stream_node is not None
        if self._stream_node is None or (
            parent is not None and self._stream_node.parent is not parent
        ):
            self._stream_node = self._new_stream_node(parent=parent)
        if phase == "thinking":
            lines = _thinking_visual_lines(clean, self._markdown_width())
            visible = lines[-5:]
            self._stream_node.header = ""
            self._stream_node.body_lines = [
                _ansi_line(f"  {escape(line)}") for line in visible
            ]
            self._stream_node.payload["phase"] = "thinking"
            if stream_existed and self._stream_node is not None:
                self._tree.mark_dirty(self._stream_node.id)
            else:
                self._tree.mark_dirty()
            return
        was_thinking_stream = self._stream_node.payload.get("phase") == "thinking"
        if clean.startswith("● "):
            clean = clean[2:]
        lines = _markdown_lines(clean, self._markdown_width())
        if not lines:
            return

        # Render the bullet and first line as one ANSI run so the terminal keeps
        # them on the same visual baseline.
        bullet = _ansi_rgb("●", (163, 190, 140))
        self._stream_node.header = _ansi_line(f"{bullet} {lines[0]}")
        self._stream_node.body_lines = [_ansi_line(f"  {line}") for line in lines[1:]]
        self._stream_node.payload.pop("phase", None)
        self._stream_node.payload["raw_text"] = clean  # 原始 markdown，供 snapshot 恢复用
        # Content-only update on existing node: mark only that subtree dirty.
        # New node (structural change): mark the whole tree dirty.
        if stream_existed and self._stream_node is not None and not was_thinking_stream:
            self._tree.mark_dirty(self._stream_node.id)
        else:
            self._tree.mark_dirty()

    def _new_stream_node(self, *, parent: OutputNode | None = None) -> OutputNode:
        target = parent or self.ensure_agent()
        if parent is None and is_agent_placeholder_header(target.header):
            target.header = ""
            target.body_lines = []
            self._tree.mark_dirty()
        return self._tree.new_node(
            parent=target,
            node_type="assistant",
            header="",
            collapsed=False,
        )

    def _settle_stream_for_tool(self) -> None:
        self.commit_stream()

    def _is_duplicate_committed_stream(self, text: str, target: OutputNode) -> bool:
        signature = _stream_signature(text)
        if not signature:
            return False
        if signature != self._last_committed_stream_text:
            return False
        if target.id != self._last_committed_stream_parent_id:
            return False
        node_id = self._last_committed_stream_node_id
        if not node_id:
            return False
        last = self._tree.get(node_id)
        return (
            last is not None
            and last.node_type == "assistant"
            and last.id in self._settled_node_ids
        )


def _thinking_visual_lines(text: str, width: int) -> list[str]:
    console = Console(width=max(width, 1), force_terminal=True, _environ={})
    lines: list[str] = []
    for logical_line in text.splitlines() or [text]:
        wrapped = Text(logical_line).wrap(console, max(width, 1))
        if wrapped:
            lines.extend(wrapped_line.plain for wrapped_line in wrapped)
        else:
            lines.append("")
    return lines


def _stream_signature(text: str) -> str:
    clean = _clean(text).strip()
    if clean.startswith("● "):
        clean = clean[2:].lstrip()
    return clean
