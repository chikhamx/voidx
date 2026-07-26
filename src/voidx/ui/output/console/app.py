"""Rich console — smooth streaming, status indicators, Claude Code style."""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from voidx.logging.tool_log import log_tool_event
from voidx.ui.output.console.formatting import (
    _capture_ansi,
    _done_spin,
    _event_tool_id,
    _fmt_args,
    _fmt_args_short,
    _next_spin,
    _pop_event_tool_id,
    _title,
    fmt_args,
)
from voidx.ui.output.dock import dock
from voidx.ui.output.events import (
    AnsiAppended,
    DiffAppended,
    ErrorAppended,
    MarkdownAppended,
    StatusUpdated,
    ToolFinished,
    ToolResultAppended,
    ToolStarted,
    ui_events,
    via_events,
)
from voidx.ui.output.console.streaming import StreamingRenderer
from voidx.ui.output.display_policy import ToolDisplayMode, ToolDisplayPolicy, ToolDisplayRule


class VoidConsole:
    """Thin wrapper with voidx-specific rendering primitives."""

    _TOOL_GERUND: dict[str, str] = {
        "read": "reading", "manage": "manage", "write": "editing", "replace": "replacing",
        "find": "finding", "search": "searching", "bash": "running", "powershell": "running",
        "agent": "delegating", "webfetch": "fetching", "websearch": "searching",
        "todo": "updating", "task_status": "checking",
        "lsp": "using",
        "checkpoint": "checking",
        "git": "git",
    }

    _AGENT_GERUND: dict[str, str] = {
        "voidx": "thinking",
        "explore": "exploring",
        "plan": "planning",
        "implement": "implementing",
        "review": "reviewing",
    }

    def __init__(self) -> None:
        self._console = Console()
        self._debug = False
        self._pending_tools: dict[str, list[dict[str, object]]] = {}
        self._event_tool_ids: dict[str, list[str]] = {}
        self._display_policy: ToolDisplayPolicy | None = None

    def _get_display_rule(self, tool_name: str) -> ToolDisplayRule:
        from voidx.ui.output.display_policy import ToolDisplayPolicy, DEFAULT_DISPLAY_RULES
        policy = self._display_policy or ToolDisplayPolicy(rules=DEFAULT_DISPLAY_RULES)
        return policy.rule_for(tool_name)

    @property
    def console(self) -> Console:
        return self._console

    @property
    def width(self) -> int:
        return self._console.width

    @property
    def debug(self) -> bool:
        return self._debug

    def set_debug(self, value: bool) -> None:
        self._debug = value

    def print(self, *args, **kwargs) -> None:
        if via_events():
            text = _capture_ansi(
                self._console.width,
                lambda console: console.print(*args, **kwargs),
            )
            if text:
                ui_events.emit_direct(AnsiAppended(text=text))
            return
        if dock.active and dock.print(*args, **kwargs):
            return
        self._console.print(*args, **kwargs)

    def markdown(self, content: str) -> None:
        if via_events():
            ui_events.emit_direct(MarkdownAppended(content=content))
            return
        if dock.active and dock.capture(lambda console: console.print(Markdown(content))):
            return
        self._console.print(Markdown(content))

    def thinking(self, text: str) -> None:
        if via_events():
            captured = _capture_ansi(
                self._console.width,
                lambda console: console.print(Text(text, style="dim italic")),
            )
            if captured:
                ui_events.emit_direct(AnsiAppended(text=captured))
            return
        if dock.active and dock.capture(lambda console: console.print(Text(text, style="dim italic"))):
            return
        self._console.print(Text(text, style="dim italic"))

    def tool_call(self, tool_name: str, args: dict[str, object]) -> None:
        if not self._debug:
            self._pending_tools.setdefault(tool_name, []).append(args)
            return
        _rule = self._get_display_rule(tool_name)
        if _rule.mode == ToolDisplayMode.HIDDEN:
            return
        gerund = _title(self._TOOL_GERUND.get(tool_name, tool_name + "ing"))
        if via_events():
            event_id = _event_tool_id(tool_name)
            self._event_tool_ids.setdefault(tool_name, []).append(event_id)
            ui_events.emit_direct(ToolStarted(
                tool_call_id=event_id,
                tool_name=tool_name,
                label=gerund,
                args=_fmt_args(args),
                raw_args=args,
                display_mode=_rule.mode,
                summary_max_lines=_rule.summary_max_lines,
            ))
            return
        if dock.active:
            dock.start_tool(gerund, _fmt_args(args), tool_name=tool_name, raw_args=args)
            return
        self.print(f"  {_next_spin()} [bold]{gerund}[/bold]({_fmt_args(args)})")

    def tool_done(self, tool_name: str, elapsed: float, ok: bool = True) -> None:
        _rule = self._get_display_rule(tool_name)
        if _rule.mode == ToolDisplayMode.HIDDEN:
            if not self._debug:
                self._pending_tools.pop(tool_name, None)
            return
        icon = _done_spin() if ok else "[red]●[/red]"
        style = "green" if ok else "red"
        label = _title(tool_name)
        if not self._debug:
            pending = self._pending_tools.get(tool_name, [])
            args = pending.pop(0) if pending else {}
            if not pending:
                self._pending_tools.pop(tool_name, None)
            elapsed_part = f" [dim]({elapsed:.1f}s)[/dim]" if elapsed >= 2 else ""
            if via_events():
                event_id = _event_tool_id(tool_name)
                detail = _fmt_args_short(tool_name, args)
                ui_events.emit_direct(ToolStarted(
                    tool_call_id=event_id,
                    tool_name=tool_name,
                    label=label,
                    args=detail,
                    raw_args=args,
                    display_mode=_rule.mode,
                    summary_max_lines=_rule.summary_max_lines,
                ))
                ui_events.emit_direct(ToolFinished(
                    tool_call_id=event_id,
                    label=label,
                    elapsed=elapsed,
                    ok=ok,
                ))
                return
            if dock.active:
                detail = _fmt_args_short(tool_name, args)
                dock.start_tool(label, detail, tool_name=tool_name, raw_args=args)
                dock.finish_tool(label, elapsed, ok)
                return
            self.print(
                f"  {icon} [{style}]{label}[/] [dim]{_fmt_args_short(tool_name, args)}[/]{elapsed_part}"
            )
            return
        if via_events():
            event_id = _pop_event_tool_id(self._event_tool_ids, tool_name)
            if event_id:
                ui_events.emit_direct(ToolFinished(
                    tool_call_id=event_id,
                    label=label,
                    elapsed=elapsed,
                    ok=ok,
                ))
                return
        if dock.active:
            dock.finish_tool(label, elapsed, ok)
            return
        self.print(f"  {icon} [{style}]{label}[/{style}] [dim]({elapsed:.1f}s)[/dim]")

    def tool_result(self, text: str, *, tool_name: str = "") -> None:
        if via_events():
            _rule = self._get_display_rule(tool_name)
            if _rule.mode == ToolDisplayMode.HIDDEN:
                return
            ui_events.emit_direct(ToolResultAppended(
                text=text,
                display_mode=_rule.mode,
                summary_max_lines=_rule.summary_max_lines,
            ))
            return
        _rule = self._get_display_rule(tool_name)
        if _rule.mode == ToolDisplayMode.HIDDEN:
            return
        if _rule.mode == ToolDisplayMode.SUMMARY:
            lines = text.splitlines()
            max_lines = _rule.summary_max_lines
            if len(lines) > max_lines:
                text = "\n".join(lines[:max_lines]) + f"\n… +{len(lines) - max_lines} more lines"
        if dock.active:
            dock.append_tool_result(text)
            return
        self.print(text)

    def error(self, message: str) -> None:
        log_tool_event("ui_error", message=message)
        if via_events():
            ui_events.emit_direct(ErrorAppended(message=message))
            return
        if dock.active:
            dock.append_error(message)
            return
        self._console.print(Panel(message, border_style="red", title="error"))

    def warn(self, message: str) -> None:
        log_tool_event("ui_warn", message=message)
        self.print(f"[yellow]! {message}[/yellow]")

    def sep(self) -> None:
        w = self._console.width or 80
        self.print("─" * w, style="dim")

    def step_header(self, agent: str = "") -> None:
        gerund = _title(self._AGENT_GERUND.get(agent, agent))
        label = f"Agent step" if agent == "voidx" else f"{gerund}"
        if via_events():
            ui_events.emit_direct(StatusUpdated(
                status_id="agent:-1:progress",
                label=label,
                stage="agent_step",
                display="record_only",
            ))
            return
        if not self._debug:
            return
        if dock.active:
            return
        self.print(f"  {_next_spin()} [dim]{gerund}[/dim]")

    def diff(self, diff_text: str, title: str = "") -> None:
        from voidx.ui.output.diff import render_diff
        if via_events():
            ui_events.emit_direct(DiffAppended(diff_text=diff_text, title=title))
            return
        if dock.active and dock.capture(lambda console: render_diff(console, diff_text, title)):
            return
        render_diff(self._console, diff_text, title)


class TreeAwareConsole:
    """Dual-write console: prints to Rich AND records to an OutputTree node.
    
    Used by the main orchestrator agent for real-time output + tree building.
    Sub-agents use CaptureConsole instead.
    """

    def __init__(self, rich_console: Console, tree, turn_node):
        self._console = rich_console
        self._tree = tree
        self._turn = turn_node
        self._current_tool = None

    @property
    def console(self) -> Console:
        return self._console

    def step_header(self, agent: str = "") -> None:
        gerund = _title(VoidConsole._AGENT_GERUND.get(agent, agent))
        self._console.print(f"  {_next_spin()} [dim]{gerund}[/dim]")

    def tool_call(self, tool_name: str, args: dict[str, object]) -> None:
        gerund = _title(VoidConsole._TOOL_GERUND.get(tool_name, tool_name + "ing"))
        dot = _next_spin()
        self._console.print(
            f"  {dot} [bold]{gerund}[/bold]({fmt_args(args)})"
        )
        self._current_tool = self._tree.new_node(
            parent=self._turn,
            node_type="tool_call",
            header=f"{dot} [bold]{gerund}[/bold]({fmt_args(args)})",
            status="running",
            collapsed=True,
        )

    def tool_done(self, tool_name: str, elapsed: float, ok: bool = True) -> None:
        icon = _done_spin() if ok else "[red]●[/red]"
        style = "green" if ok else "red"
        label = _title(tool_name)
        self._console.print(
            f"  {icon} [{style}]{label}[/{style}] [dim]({elapsed:.1f}s)[/dim]"
        )
        if self._current_tool:
            self._current_tool.header += f"  [{style}]{label} ({elapsed:.1f}s)[/{style}]"
            self._current_tool.elapsed = elapsed
            self._current_tool.status = "done" if ok else "error"
            self._tree.mark_dirty()

    def tool_result(self, text: str) -> None:
        self._console.print(text)
        if self._current_tool:
            self._tree.new_node(
                parent=self._current_tool,
                node_type="tool_result",
                body_lines=text.split("\n"),
                collapsed=False,
            )

    def diff(self, diff_text: str, title: str = "") -> None:
        from voidx.ui.output.diff import render_diff
        render_diff(self._console, diff_text, title)
        if self._current_tool:
            lines = diff_text.split("\n")
            if title:
                lines.insert(0, f"[bold]{title}[/bold]")
            self._tree.new_node(
                parent=self._current_tool,
                node_type="tool_result",
                body_lines=lines,
                collapsed=False,
            )

    def print(self, *args, **kwargs) -> None:
        self._console.print(*args, **kwargs)

    def warn(self, message: str) -> None:
        self._console.print(f"[yellow]! {message}[/yellow]")

    def error(self, message: str) -> None:
        from rich.panel import Panel
        self._console.print(Panel(message, border_style="red", title="error"))

    def sep(self) -> None:
        w = self._console.width or 80
        self._console.print("─" * w, style="dim")
