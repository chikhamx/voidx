"""Output node mutation mixin for BottomInputDock."""

from __future__ import annotations

from typing import Any

from rich.markup import escape

from voidx.ui.dock_parts.formatting import (
    _ansi_line,
    _clean,
    _short_path,
    _short_value,
    _strip_ansi_trailing_space,
    _tail_lines,
)
from voidx.ui.tree import OutputNode


class DockNodeMixin:
    def append_startup(
        self,
        *,
        model: str,
        provider: str,
        workspace: str,
        session_title: str,
        is_new: bool,
        profile_configured: bool = True,
    ) -> OutputNode | None:
        from voidx.ui.startup import render_startup_lines

        lines = render_startup_lines(
            self._width(),
            model=model,
            provider=provider,
            workspace=workspace,
            session_title=session_title,
            is_new=is_new,
        )
        if not profile_configured:
            lines.extend([
                "[yellow]No profile configured — chat is disabled until you set one up.[/yellow]",
                "[dim]  Use [cyan]/model config[/cyan] to create a profile interactively[/dim]",
            ])
        if not lines:
            return None
        node = self._tree.new_node(
            parent=self._tree.root,
            node_type="startup",
            header=lines[0],
            body_lines=lines[1:],
            collapsed=False,
        )
        self.refresh()
        return node

    def append_message(self, text: str, *, style: str = "", parent: OutputNode | None = None) -> OutputNode | None:
        clean = _clean(text)
        if not clean.strip():
            return None
        target = parent or self._tree.root
        lines = [_strip_ansi_trailing_space(line) for line in (clean.splitlines() or [clean])]
        header = escape(lines[0])
        if style:
            header = f"[{style}]{header}[/]"
        body_lines = [escape(line) for line in lines[1:]]
        if style:
            body_lines = [f"[{style}]{line}[/]" for line in body_lines]
        node = self._tree.new_node(
            parent=target,
            node_type="message",
            header=header,
            body_lines=body_lines,
            collapsed=False,
        )
        self.refresh()
        return node

    def append_error(self, message: str, *, parent: OutputNode | None = None) -> OutputNode | None:
        clean = _clean(message)
        if not clean.strip():
            return None
        lines = [_strip_ansi_trailing_space(line) for line in (clean.splitlines() or [clean])]
        node = self._tree.new_node(
            parent=parent or self._tree.root,
            node_type="error",
            header=f"[red]✗ {escape(lines[0])}[/red]",
            body_lines=[f"[red]  {escape(line)}[/red]" for line in lines[1:]],
            collapsed=False,
            status="error",
        )
        self.refresh()
        return node

    def append_ansi(self, text: str, *, parent: OutputNode | None = None) -> OutputNode | None:
        clean = text.rstrip("\n")
        if not clean.strip():
            return None
        lines = [_strip_ansi_trailing_space(line) for line in (clean.splitlines() or [clean])]
        node = self._tree.new_node(
            parent=parent or self._tree.root,
            node_type="message",
            header=_ansi_line(lines[0]),
            body_lines=[_ansi_line(line) for line in lines[1:]],
            collapsed=False,
        )
        self.refresh()
        return node

    def append_thought(self, text: str, elapsed: float | None = None) -> OutputNode | None:
        clean = _clean(text).strip()
        if not clean:
            return None
        lines = clean.splitlines()
        summary = f"Thinking for {elapsed:.0f}s" if elapsed is not None else "Thinking"
        if lines:
            summary += f", {len(lines)} line{'s' if len(lines) != 1 else ''}"
        node = self._tree.new_node(
            parent=self.ensure_agent(),
            node_type="thought",
            header=f"[dim]●[/dim] [dim]{escape(summary)}[/dim]",
            body_lines=[f"[dim]{escape(line)}[/dim]" for line in lines[-5:]],
            collapsed=True,
            meta=summary,
        )
        self.refresh()
        return node

    def start_tool(self, label: str, args: str = "", *, parent: OutputNode | None = None) -> OutputNode:
        if parent is None:
            self._settle_stream_for_tool()
        header = f"[bold]{escape(label)}[/]"
        if args:
            header += f"({args})"
        parent = parent or self.ensure_agent()
        tool_body = header
        self._current_tool = self._tree.new_node(
            parent=parent,
            node_type="tool_call",
            header=f"[#A3BE8C]●[/#A3BE8C] {tool_body}",
            status="running",
            collapsed=False,
            meta=tool_body,
        )
        self.refresh()
        return self._current_tool

    def finish_tool(self, label: str, elapsed: float, ok: bool = True, detail: str = "") -> None:
        if not self._current_tool:
            return
        self.finish_tool_node(self._current_tool, label, elapsed, ok, detail)

    def finish_tool_node(
        self,
        node: OutputNode,
        label: str,
        elapsed: float,
        ok: bool = True,
        detail: str = "",
    ) -> None:
        color = "dim" if ok else "red"
        icon = "●" if ok else "✗"
        tool_body = node.meta or node.header
        suffix = f" [dim]({elapsed:.1f}s)[/dim]"
        if detail:
            suffix += f" [dim]{detail}[/dim]"
        node.header = f"[{color}]{icon}[/{color}] {tool_body}{suffix}"
        node.elapsed = elapsed
        node.status = "done" if ok else "error"
        self._tree.mark_dirty()
        self.refresh()

    def append_tool_result(
        self,
        text: str,
        *,
        parent: OutputNode | None = None,
        collapsed: bool = True,
    ) -> OutputNode | None:
        clean = _clean(text)
        if not clean.strip():
            return None
        lines = [_strip_ansi_trailing_space(line) for line in (clean.splitlines() or [clean])]
        while lines and not _clean(lines[0]).strip():
            lines.pop(0)
        while lines and not _clean(lines[-1]).strip():
            lines.pop()
        if not lines:
            return None
        node = self._tree.new_node(
            parent=parent or self._current_tool or self._current_agent or self._tree.root,
            node_type="tool_result",
            header=escape(lines[0]) if lines else "",
            body_lines=[escape(line) for line in lines[1:]],
            collapsed=collapsed,
        )
        self.refresh()
        return node

    def append_file_change(
        self,
        diff_text: str,
        *,
        parent: OutputNode | None = None,
        preview_hunks: int = 1,
        preview_lines: int = 8,
    ) -> OutputNode | None:
        from voidx.ui.diff import (
            parse_unified_diff,
            render_file_change_lines,
            render_full_file_diff_lines,
        )

        parsed = parse_unified_diff(diff_text)
        if not parsed.files:
            return self.append_tool_result(diff_text, parent=parent, collapsed=True)

        target = parent or self._current_tool or self._current_agent or self._tree.root
        first_node: OutputNode | None = None
        for index, file_diff in enumerate(parsed.files):
            body_lines, omitted = render_file_change_lines(file_diff, preview_hunks, preview_lines)
            header = (
                f"[#A3BE8C]●[/#A3BE8C] "
                f"[bold]{file_diff.operation}[/bold]({escape(_short_path(file_diff.path))})"
            )
            if index == 0 and target.node_type == "tool_call":
                node = target
                node.header = header
                node.body_lines = body_lines
                node.collapsed = False
                node.status = "done"
                node.meta = header
            else:
                node = self._tree.new_node(
                    parent=target,
                    node_type="tool_call",
                    header=header,
                    body_lines=body_lines,
                    collapsed=False,
                    status="done",
                    meta=header,
                )
            full_lines = render_full_file_diff_lines(file_diff)
            if omitted and full_lines:
                self._tree.new_node(
                    parent=node,
                    node_type="tool_result",
                    header="[dim]Full diff[/dim]",
                    body_lines=full_lines,
                    collapsed=True,
                )
            if first_node is None:
                first_node = node
        self._tree.mark_dirty()
        self.refresh()
        return first_node

    def set_status(
        self,
        status_id: str,
        label: str,
        detail: str = "",
        *,
        parent: OutputNode | None = None,
        stage: str = "working",
    ) -> OutputNode:
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
        node = self._status_nodes.pop(status_id, None)
        if node is None:
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
        self.refresh()

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
        self.refresh()
        return self._permission_node

    def clear_permission(self) -> None:
        if self._permission_node is None:
            return
        self._remove_node(self._permission_node)
        self._permission_node = None
        self.refresh()
