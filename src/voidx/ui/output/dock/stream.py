"""Streaming output node management for BottomInputDock."""

from __future__ import annotations

from rich.markup import escape

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
        stream_node = self._stream_node
        if stream_node is not None and stream_node is not self._current_agent:
            self._mark_settled(stream_node)
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
        stream_existed = self._stream_node is not None
        if self._stream_node is None or (
            parent is not None and self._stream_node.parent is not parent
        ):
            self._stream_node = self._new_stream_node(parent=parent)
        if phase == "thinking":
            lines = clean.splitlines()
            visible = lines[-5:]
            bullet = _ansi_rgb("⏳", (235, 203, 139))
            self._stream_node.header = _ansi_line(f"{bullet} Thinking")
            self._stream_node.body_lines = [
                _ansi_line(f"  {escape(line)}") for line in visible
            ]
            if stream_existed and self._stream_node is not None:
                self._tree.mark_dirty(self._stream_node.id)
            else:
                self._tree.mark_dirty()
            return
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
        # Content-only update on existing node: mark only that subtree dirty.
        # New node (structural change): mark the whole tree dirty.
        if stream_existed and self._stream_node is not None:
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
