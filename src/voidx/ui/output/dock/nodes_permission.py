"""Permission prompt node mutations for BottomInputDock."""

from __future__ import annotations

from typing import Any

from rich.markup import escape

from voidx.ui.output.dock.formatting import _short_value
from voidx.ui.output.tree import OutputNode


class DockPermissionNodeMixin:
    def show_permission(
        self,
        prompt: str,
        tools: list[dict[str, Any]],
        *,
        parent: OutputNode | None = None,
    ) -> OutputNode:
        self.clear_permission()
        body: list[str] = []
        for index, tool in enumerate(tools, 1):
            name = str(tool.get("name") or "tool")
            pattern = str(tool.get("pattern") or "")
            body.append(escape(f"{index}. {name}"))
            if pattern and pattern != "*":
                body.append(escape(f"   target: {pattern}"))
            args = tool.get("args")
            if isinstance(args, dict):
                for key, value in args.items():
                    body.append(escape(f"   {key}: {_short_value(value)}"))
        self._permission_node = self._tree.new_node(
            parent=parent or self._tree.root,
            node_type="permission",
            header=f"[yellow]Permission required[/yellow] {escape(prompt)}",
            body_lines=body,
            collapsed=False,
        )
        self._mark_unsettled(self._permission_node)
        self.refresh()
        return self._permission_node

    def clear_permission(self) -> None:
        if self._permission_node is None:
            return
        self._remove_node(self._permission_node)
        self._permission_node = None
        self.refresh()

