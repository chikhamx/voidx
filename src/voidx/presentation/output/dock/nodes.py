"""Output node mutation mixin for BottomInputDock."""

from __future__ import annotations

import re
from typing import Any

from rich.markup import escape

from voidx.presentation.output.dock.formatting import (
    _ansi_line,
    _clean,
    _markdown_lines,
    short_path,
    short_value,
    _strip_ansi_trailing_space,
    _tail_lines,
)
from voidx.presentation.output.agent_display import agent_display_name
from voidx.presentation.output.manage_display import manage_display
from voidx.presentation.output.tool_display import extract_tool_display_value, mcp_gateway_tool_name
from voidx.presentation.output.tree import OutputNode
from voidx.presentation.output.dock.nodes_startup import DockStartupNodeMixin
from voidx.presentation.output.dock.nodes_status import DockStatusNodeMixin
from voidx.presentation.output.dock.nodes_permission import DockPermissionNodeMixin
from voidx.presentation.output.dock.nodes_checkpoint import DockCheckpointNodeMixin
from voidx.presentation.output.dock.nodes_clarify import DockClarifyNodeMixin
from voidx.presentation.output.dock.nodes_goal_spec import DockGoalSpecNodeMixin


class DockNodeMixin(
    DockStartupNodeMixin,
    DockStatusNodeMixin,
    DockPermissionNodeMixin,
    DockCheckpointNodeMixin,
    DockClarifyNodeMixin,
    DockGoalSpecNodeMixin,
):
    def _new_completed_node(
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
        self._mark_completed(node)
        return node

    def append_message(
        self,
        text: str,
        *,
        style: str = "",
        parent: OutputNode | None = None,
        markup: bool = False,
        title: str = "",
    ) -> OutputNode | None:
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
        payload: dict[str, str] = {}
        if style:
            payload["style"] = style
        if title:
            payload["title"] = title
        payload["raw_text"] = clean
        node = self._new_completed_node(
            target,
            before_active_stream=parent is None,
            node_type="message",
            header=header,
            body_lines=body_lines,
            collapsed=False,
            payload=payload,
        )
        if parent is None and self._stream_node is None:
            self._current_agent = None
            self._current_tool = None
        self.refresh()
        return node

    def append_error(self, message: str, *, parent: OutputNode | None = None) -> OutputNode | None:
        clean = _clean(message)
        if not clean.strip():
            return None
        lines = [_strip_ansi_trailing_space(line) for line in (clean.splitlines() or [clean])]
        node = self._new_completed_node(
            parent or self._tree.root,
            before_active_stream=parent is None,
            node_type="error",
            header=f"[red]✗ {escape(lines[0])}[/red]",
            body_lines=[f"[red]  {escape(line)}[/red]" for line in lines[1:]],
            collapsed=False,
            status="error",
            payload={"raw_text": clean},
        )
        self.refresh()
        return node

    def append_ansi(self, text: str, *, parent: OutputNode | None = None) -> OutputNode | None:
        clean = text.rstrip("\n")
        if not clean.strip():
            return None
        lines = [_strip_ansi_trailing_space(line) for line in (clean.splitlines() or [clean])]
        node = self._new_completed_node(
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
            payload={"raw_text": clean},
        )
        self._mark_completed(node)
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
        if tool_name in ("bash", "powershell"):
            command = str(raw_args.get("command") or "")
            if command:
                body_lines = bash_markdown_lines(command, self._markdown_width())
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
        self._mark_completed(node, outcome="completed" if ok else "failed")
        self._tree.mark_dirty()
        self.refresh()

    def _result_parent(self, target: OutputNode) -> OutputNode:
        if (
            target.node_type == "tool_call"
            and target.payload.get("tool_name") != "agent"
            and target.parent is not None
        ):
            return target.parent
        return target

    def _find_tool_result(
        self,
        parent: OutputNode,
        *,
        tool_call_id: str | None,
        anchor_id: str | None,
    ) -> OutputNode | None:
        for node in parent.children:
            if node.node_type != "tool_result" or not node.payload.get("independent_result"):
                continue
            if tool_call_id is not None:
                if node.payload.get("result_tool_call_id") == tool_call_id:
                    return node
            elif anchor_id is not None and node.payload.get("result_anchor_id") == anchor_id:
                return node
        return None

    def _find_diff_result(
        self,
        parent: OutputNode,
        *,
        tool_call_id: str | None,
        anchor_id: str | None,
        index: int,
        path: str,
    ) -> OutputNode | None:
        for node in parent.children:
            if node.node_type != "tool_call" or not node.payload.get("diff_result"):
                continue
            matches_call = (
                node.payload.get("result_tool_call_id") == tool_call_id
                if tool_call_id is not None
                else node.payload.get("result_anchor_id") == anchor_id
            )
            if matches_call and (
                node.payload.get("diff_index") == index
                or node.payload.get("diff_path") == path
            ):
                return node
        return None

    def _ensure_result_spacer(self, result: OutputNode) -> OutputNode | None:
        parent = result.parent
        if parent is None:
            return None
        spacers = [
            node
            for node in parent.children
            if node.node_type == "message"
            and node.payload.get("tool_result_spacer_for") == result.id
        ]
        spacer = spacers[0] if spacers else None
        for duplicate in spacers[1:]:
            self._remove_node(duplicate)
        if spacer is None:
            next_index = parent.children.index(result) + 1
            kwargs = {
                "parent": parent,
                "node_type": "message",
                "header": "",
                "collapsed": False,
                "payload": {"tool_result_spacer_for": result.id},
            }
            if next_index < len(parent.children):
                spacer = self._tree.new_node_before(parent.children[next_index], **kwargs)
            else:
                spacer = self._tree.new_node(**kwargs)
        else:
            current_index = parent.children.index(spacer)
            result_index = parent.children.index(result)
            desired_index = result_index + 1
            if current_index != desired_index:
                parent.children.pop(current_index)
                if current_index < desired_index:
                    desired_index -= 1
                parent.children.insert(desired_index, spacer)
                self._tree._refresh_sibling_flags(parent)
                self._tree.mark_dirty()
        self._mark_completed(spacer)
        return spacer

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
        result_parent = self._result_parent(target)
        result_tool_call_id = tool_call_id or (
            target.tool_call_id if target.node_type == "tool_call" else None
        )
        result_anchor_id = target.id if target.node_type == "tool_call" else None
        node = self._find_tool_result(
            result_parent,
            tool_call_id=result_tool_call_id,
            anchor_id=result_anchor_id,
        )
        if node is None:
            node = self._tree.new_node(
                parent=result_parent,
                node_type="tool_result",
                header=escape(lines[0]),
                body_lines=[escape(line) for line in lines[1:]],
                collapsed=collapsed,
                status="done",
                tool_call_id=result_tool_call_id,
                payload={
                    "raw_text": clean,
                    "independent_result": True,
                    "result_tool_call_id": result_tool_call_id,
                    "result_anchor_id": result_anchor_id,
                },
            )
        else:
            node.header = escape(lines[0])
            node.body_lines = [escape(line) for line in lines[1:]]
            node.collapsed = collapsed
            node.status = "done"
            if result_tool_call_id is not None:
                node.tool_call_id = result_tool_call_id
            node.payload.update(
                raw_text=clean,
                result_tool_call_id=result_tool_call_id,
                result_anchor_id=result_anchor_id,
            )
        self._mark_completed(node)
        self._ensure_result_spacer(node)
        self._tree.mark_dirty()
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
        from voidx.presentation.output.diff import (
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
        result_parent = self._result_parent(target)
        result_tool_call_id = tool_call_id or (
            target.tool_call_id if target.node_type == "tool_call" else None
        )
        result_anchor_id = target.id if target.node_type == "tool_call" else None
        first_node: OutputNode | None = None
        completed_nodes: list[OutputNode] = []
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
            node = self._find_diff_result(
                result_parent,
                tool_call_id=result_tool_call_id,
                anchor_id=result_anchor_id,
                index=index,
                path=file_diff.path,
            )
            if node is None:
                node = self._tree.new_node(
                    parent=result_parent,
                    node_type="tool_call",
                    header=header,
                    body_lines=body_lines,
                    collapsed=not show_diff,
                    status="done",
                    meta=header,
                    tool_call_id=result_tool_call_id,
                    payload={
                        "diff_text": diff_text,
                        "diff_result": True,
                        "independent_result": True,
                        "result_tool_call_id": result_tool_call_id,
                        "result_anchor_id": result_anchor_id,
                        "diff_index": index,
                        "diff_path": file_diff.path,
                    },
                )
            else:
                node.header = header
                node.body_lines = body_lines
                node.collapsed = not show_diff
                node.status = "done"
                node.meta = header
                if result_tool_call_id is not None:
                    node.tool_call_id = result_tool_call_id
                node.payload.update(
                    diff_text=diff_text,
                    result_tool_call_id=result_tool_call_id,
                    result_anchor_id=result_anchor_id,
                    diff_index=index,
                    diff_path=file_diff.path,
                )
            if omitted:
                full_lines = render_full_file_diff_lines(file_diff)
                if full_lines:
                    full_diff = next(
                        (
                            child
                            for child in node.children
                            if child.node_type == "tool_result"
                            and child.payload.get("full_diff_result")
                        ),
                        None,
                    )
                    if full_diff is None:
                        full_diff = self._tree.new_node(
                            parent=node,
                            node_type="tool_result",
                            header="[dim]Full diff[/dim]",
                            body_lines=full_lines,
                            collapsed=True,
                            status="done",
                            payload={"full_diff_result": True},
                        )
                    else:
                        full_diff.body_lines = full_lines
                    self._mark_completed(full_diff)
            if first_node is None:
                first_node = node
            completed_nodes.append(node)
        for node in completed_nodes:
            self._mark_completed(node)
        if completed_nodes:
            self._ensure_result_spacer(completed_nodes[-1])
        self._tree.mark_dirty()
        self.refresh()
        return first_node



def bash_markdown_lines(command: str, width: int) -> list[str]:
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
    if tool_name in {"agent", "agent_control"}:
        action = str(raw_args.get("action") or "spawn").strip().lower()
        if action in {"wait", "cancel"}:
            name = "Wait" if action == "wait" else "Cancel"
            value = extract_tool_display_value(tool_name, raw_args, args)
            if value:
                return f'[bold]{escape(name)}[/bold]("[cyan]{escape(_shorten(value))}[/cyan]")'
            return f"[bold]{escape(name)}[/bold]()"
        agent_name = raw_args.get("name") or extract_tool_display_value(tool_name, raw_args, args)
        return f"[bold]{escape(agent_display_name(agent_name))}[/bold]"
    if tool_name == "manage":
        name, value = manage_display(raw_args, limit=56)
        if value:
            return f'[bold]{escape(name)}[/bold]("[cyan]{escape(value)}[/cyan]")'
        return f"[bold]{escape(name)}[/bold]()"
    if tool_name == "mcp":
        name = mcp_gateway_tool_name(raw_args)
    else:
        name = _tool_display_name(tool_name, label)
    value = extract_tool_display_value(tool_name, raw_args, args)
    if value:
        return f'[bold]{escape(name)}[/bold]("[cyan]{escape(_shorten(value))}[/cyan]")'
    return f"[bold]{escape(name)}[/bold]()"


def _operation_header(operation: str, path: str) -> str:
    return f'[bold]{escape(operation)}[/bold]("[cyan]{escape(short_path(path))}[/cyan]")'


def _tool_display_name(tool_name: str, label: str) -> str:
    mapping = {
        "read": "Read",
        "search": "Search",
        "find": "Search",
        "manage": "Manage",
        "write": "Update",
        "replace": "Update",
        "lsp": "Lsp",
        "bash": "Bash",
        "powershell": "PowerShell",
        "webfetch": "Fetch",
        "websearch": "Search",
        "todo": "Todo",
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




def _shorten(text: str, limit: int = 80) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"
