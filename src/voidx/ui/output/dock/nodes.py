"""Output node mutation mixin for BottomInputDock."""

from __future__ import annotations

import re
from typing import Any

from rich.markup import escape

from voidx.ui.output.dock.formatting import (
    _ansi_line,
    _clean,
    _markdown_lines,
    _short_path,
    _short_value,
    _strip_ansi_trailing_space,
    _tail_lines,
)
from voidx.ui.output.agent_display import agent_display_name
from voidx.ui.output.tree import OutputNode
from voidx.ui.output.dock.nodes_startup import DockStartupNodeMixin
from voidx.ui.output.dock.nodes_status import DockStatusNodeMixin
from voidx.ui.output.dock.nodes_permission import DockPermissionNodeMixin


class DockNodeMixin(DockStartupNodeMixin, DockStatusNodeMixin, DockPermissionNodeMixin):
    def _new_settled_node(
        self,
        target: OutputNode,
        *,
        before_active_stream: bool,
        **kwargs: Any,
    ) -> OutputNode:
        reference = self._stream_node if before_active_stream else None
        if reference is not None and reference.parent is not None:
            node = self._tree.new_node_before(reference, **kwargs)
        else:
            node = self._tree.new_node(parent=target, **kwargs)
        self._mark_settled(node)
        return node

    def append_message(self, text: str, *, style: str = "", parent: OutputNode | None = None, markup: bool = False) -> OutputNode | None:
        clean = _clean(text)
        if not clean.strip():
            return None
        target = parent or self._tree.root
        lines = [_strip_ansi_trailing_space(line) for line in (clean.splitlines() or [clean])]
        header = lines[0] if markup else escape(lines[0])
        if style:
            header = f"[{style}]{header}[/]"
        body_lines = lines[1:] if markup else [escape(line) for line in lines[1:]]
        if style:
            body_lines = [f"[{style}]{line}[/]" for line in body_lines]
        node = self._new_settled_node(
            target,
            before_active_stream=parent is None,
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
        node = self._new_settled_node(
            parent or self._tree.root,
            before_active_stream=parent is None,
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
        node = self._new_settled_node(
            parent or self._tree.root,
            before_active_stream=parent is None,
            node_type="message",
            header=_ansi_line(lines[0]),
            body_lines=[_ansi_line(line) for line in lines[1:]],
            collapsed=False,
        )
        self.refresh()
        return node

    def append_thought(
        self,
        text: str,
        elapsed: float | None = None,
        *,
        parent: OutputNode | None = None,
    ) -> OutputNode | None:
        clean = _clean(text).strip()
        if not clean:
            return None
        lines = clean.splitlines()
        summary = f"Thinking for {elapsed:.0f}s" if elapsed is not None else "Thinking"
        if lines:
            summary += f", {len(lines)} line{'s' if len(lines) != 1 else ''}"
        visible_lines = lines[:5]
        body: list[str] = [f"[dim]{escape(line)}[/dim]" for line in visible_lines]
        if len(lines) > 5:
            body.append(f"[dim]… (+{len(lines) - 5} more lines)[/dim]")
        node = self._tree.new_node(
            parent=parent or self.ensure_agent(),
            node_type="thought",
            header=f"[dim]●[/dim] [dim]{escape(summary)}[/dim]",
            body_lines=body,
            collapsed=False,
            meta=summary,
        )
        self._mark_settled(node)
        self.refresh()
        return node

    def start_tool(
        self,
        label: str,
        args: str = "",
        *,
        parent: OutputNode | None = None,
        tool_call_id: str | None = None,
        tool_name: str = "",
        raw_args: dict[str, Any] | None = None,
    ) -> OutputNode:
        if parent is None:
            self._settle_stream_for_tool()
        raw_args = raw_args or {}
        body_lines: list[str] = []
        if tool_name == "bash":
            command = str(raw_args.get("command") or "")
            if command:
                body_lines = _bash_markdown_lines(command, self._markdown_width())
        header = _tool_header(tool_name, label, args, raw_args)
        parent = parent or self.ensure_agent()
        tool_body = header
        self._current_tool = self._tree.new_node(
            parent=parent,
            node_type="tool_call",
            header=f"[#A3BE8C]●[/#A3BE8C] {tool_body}",
            body_lines=body_lines,
            status="running",
            collapsed=True,
            meta=tool_body,
            tool_call_id=tool_call_id,
            payload={"tool_name": tool_name, "args": args, "raw_args": raw_args},
        )
        self._mark_unsettled(self._current_tool)
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
        suffix = f" [dim]({elapsed:.1f}s)[/dim]" if elapsed >= 2 else ""
        if detail:
            suffix += f" [dim]{detail}[/dim]"
            node.payload["summary"] = detail
        node.header = f"[{color}]{icon}[/{color}] {tool_body}{suffix}"
        node.elapsed = elapsed
        node.status = "done" if ok else "error"
        self._mark_settled(node)
        self._tree.mark_dirty()
        self.refresh()

    def append_tool_result(
        self,
        text: str,
        *,
        parent: OutputNode | None = None,
        collapsed: bool = False,
        tool_call_id: str | None = None,
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
        target = parent or self._current_tool or self._current_agent or self._tree.root
        node = self._tree.new_node(
            parent=target,
            node_type="tool_result",
            header=escape(lines[0]) if lines else "",
            body_lines=[escape(line) for line in lines[1:]],
            collapsed=collapsed,
            tool_call_id=tool_call_id,
        )
        self._mark_subtree_settled(node)
        if target.node_type == "tool_call":
            self._mark_subtree_settled(target)
        self.refresh()
        return node

    def append_file_change(
        self,
        diff_text: str,
        *,
        parent: OutputNode | None = None,
        tool_call_id: str | None = None,
        preview_hunks: int | None = None,
        preview_lines: int | None = None,
    ) -> OutputNode | None:
        from voidx.ui.output.diff import (
            parse_unified_diff,
            render_file_change_lines,
            render_full_file_diff_lines,
        )

        parsed = parse_unified_diff(diff_text)
        if not parsed.files:
            return self.append_tool_result(
                diff_text,
                parent=parent,
                collapsed=True,
                tool_call_id=tool_call_id,
            )

        target = parent or self._current_tool or self._current_agent or self._tree.root
        if target.node_type == "tool_call":
            self._mark_unsettled(target)
        first_node: OutputNode | None = None
        settled_nodes: list[OutputNode] = []
        for index, file_diff in enumerate(parsed.files):
            if preview_hunks is not None and preview_lines is not None:
                body_lines, omitted = render_file_change_lines(file_diff, preview_hunks, preview_lines)
            else:
                body_lines = render_full_file_diff_lines(file_diff)
                omitted = False
            header = (
                f"[#A3BE8C]●[/#A3BE8C] "
                f"{_operation_header(file_diff.operation, file_diff.path)}"
            )
            show_diff = file_diff.operation == "Update"
            if index == 0 and target.node_type == "tool_call":
                node = target
                node.header = header
                node.body_lines = body_lines
                node.collapsed = not show_diff
                node.status = "done"
                node.meta = header
                if tool_call_id:
                    node.tool_call_id = tool_call_id
                node.payload["diff_text"] = diff_text
            else:
                node = self._tree.new_node(
                    parent=target,
                    node_type="tool_call",
                    header=header,
                    body_lines=body_lines,
                    collapsed=not show_diff,
                    status="done",
                    meta=header,
                    tool_call_id=tool_call_id,
                    payload={"diff_text": diff_text},
                )
            if omitted:
                full_lines = render_full_file_diff_lines(file_diff)
                if full_lines:
                    self._tree.new_node(
                        parent=node,
                        node_type="tool_result",
                        header="[dim]Full diff[/dim]",
                        body_lines=full_lines,
                        collapsed=True,
                    )
            if first_node is None:
                first_node = node
            settled_nodes.append(node)
        for node in settled_nodes:
            self._mark_subtree_settled(node)
        self._tree.mark_dirty()
        self.refresh()
        return first_node



def _bash_markdown_lines(command: str, width: int) -> list[str]:
    command = command.rstrip("\n")
    if not command:
        return []
    fence = _markdown_fence(command)
    markdown = f"{fence}bash\n{command}\n{fence}"
    return [_ansi_line(line) for line in _markdown_lines(markdown, width)]


def _markdown_fence(text: str) -> str:
    runs = [len(match.group(0)) for match in re.finditer(r"`+", text)]
    return "`" * max(3, max(runs, default=0) + 1)


def _tool_header(
    tool_name: str,
    label: str,
    args: str,
    raw_args: dict[str, Any],
) -> str:
    if tool_name == "agent":
        agent_name = raw_args.get("agent") or _tool_display_value(tool_name, args, raw_args)
        return f"[bold]{escape(agent_display_name(agent_name))}[/bold]"
    name = _tool_display_name(tool_name, label)
    value = _tool_display_value(tool_name, args, raw_args)
    if value:
        return f'[bold]{escape(name)}[/bold]("[cyan]{escape(_shorten(value))}[/cyan]")'
    return f"[bold]{escape(name)}[/bold]()"


def _operation_header(operation: str, path: str) -> str:
    return f'[bold]{escape(operation)}[/bold]("[cyan]{escape(_short_path(path))}[/cyan]")'


def _tool_display_name(tool_name: str, label: str) -> str:
    mapping = {
        "read": "Read",
        "grep": "Search",
        "glob": "Search",
        "edit": "Update",
        "write": "Update",
        "lsp": "Lsp",
        "bash": "Bash",
        "agent": "Agent",
        "webfetch": "Fetch",
        "websearch": "Search",
        "repo_map": "Map",
        "todo": "Todo",
        "task_status": "Status",
        "checkpoint": "Checkpoint",
    }
    label_mapping = {
        "Reading": "Read",
        "Editing": "Update",
        "Writing": "Update",
        "Searching": "Search",
        "Finding": "Search",
        "Mapping": "Map",
        "Running": "Run",
    }
    if tool_name in mapping:
        return mapping[tool_name]
    if label in label_mapping:
        return label_mapping[label]
    return label or (tool_name or "Tool").title()


def _tool_display_value(tool_name: str, args: str, raw_args: dict[str, Any]) -> str:
    value: object = ""
    if tool_name in {"read", "edit", "write", "lsp"}:
        value = raw_args.get("file_path") or raw_args.get("path")
    elif tool_name == "grep":
        pattern = raw_args.get("pattern") or raw_args.get("query")
        include = raw_args.get("include")
        value = f"{pattern} in {include}" if pattern and include else pattern
    elif tool_name == "glob":
        value = raw_args.get("pattern")
    elif tool_name == "bash":
        value = str(raw_args.get("command") or "").replace("\n", "; ")
    elif tool_name == "agent":
        value = raw_args.get("agent") or raw_args.get("description")
    elif tool_name == "checkpoint":
        value = raw_args.get("plan_summary")
    elif tool_name in {"webfetch", "websearch"}:
        value = raw_args.get("url") or raw_args.get("query")
    elif raw_args:
        for key in ("file_path", "path", "pattern", "query", "url", "command", "name"):
            if raw_args.get(key):
                value = raw_args[key]
                break
    if value:
        return str(value)
    return _strip_rich_markup(args)


def _strip_rich_markup(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\[/?[A-Za-z0-9_#= .:-]+\]", "", text)
    text = text.strip()
    if "=" in text:
        text = text.split("=", 1)[1].strip()
    return text.strip("\"'")


def _shorten(text: str, limit: int = 80) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"
