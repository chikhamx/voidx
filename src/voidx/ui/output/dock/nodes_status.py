"""Status node mutations for BottomInputDock."""

from __future__ import annotations

from rich.markup import escape

from voidx.ui.output.dock.formatting import _clean, _tail_lines
from voidx.ui.output.tree import OutputNode


class DockStatusNodeMixin:
    def set_status(
        self,
        status_id: str,
        label: str,
        detail: str = "",
        *,
        parent: OutputNode | None = None,
        stage: str = "working",
    ) -> OutputNode:
        self.record_status(status_id, label, detail, stage=stage)
        node = self._status_nodes.get(status_id)
        if node is None:
            node = self._tree.new_node(
                parent=parent or self._tree.root,
                node_type="status",
                header="",
                collapsed=False,
            )
        self._status_nodes[status_id] = node
        tick = self._status_ticks.get(status_id, 0)
        self._status_ticks[status_id] = tick + 1
        color = "#EBCB8B" if tick % 2 == 0 else "#F6D365"
        node.header = f"[{color}]●[/{color}] {escape(label)}"
        clean_detail = _clean(detail).strip()
        node.body_lines = [f"[dim]{escape(line)}[/dim]" for line in _tail_lines(clean_detail, 5)]
        node.collapsed = False
        node.status = "running"
        node.meta = label
        self._tree.mark_dirty()
        self._mark_unsettled(node)
        self.refresh()
        return node

    def finish_status(
        self,
        status_id: str,
        *,
        label: str = "",
        detail: str = "",
        ok: bool = True,
        remove: bool = True,
    ) -> None:
        had_record = status_id in self._status_records
        self.clear_status_record(status_id)
        node = self._status_nodes.pop(status_id, None)
        if node is None:
            if status_id and not had_record:
                import logging
                logging.getLogger("voidx.ui").debug("finish_status: unknown status_id=%s", status_id)
            return
        self._status_ticks.pop(status_id, None)
        if remove:
            self._remove_node(node)
            self.refresh()
            return
        color = "dim" if ok else "red"
        icon = "●" if ok else "✗"
        text = label or _clean(node.header).strip() or "Done"
        node.header = f"[{color}]{icon}[/{color}] [dim]{escape(text)}[/dim]"
        clean_detail = _clean(detail).strip()
        if clean_detail:
            node.body_lines = [f"[dim]{escape(line)}[/dim]" for line in _tail_lines(clean_detail, 5)]
        node.status = "done" if ok else "error"
        node.collapsed = True
        node.meta = text
        self._tree.mark_dirty()
        self._mark_subtree_settled(node)
        self.refresh()
