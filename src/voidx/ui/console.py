"""Rich console — smooth streaming, status indicators, Claude Code style."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from voidx.ui.console_parts.formatting import (
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
from voidx.ui.dock import dock
from voidx.ui.events import (
    AnsiAppended,
    DiffAppended,
    ErrorAppended,
    MarkdownAppended,
    StatusUpdated,
    ToolFinished,
    ToolResultAppended,
    ToolStarted,
    ui_events,
)
from voidx.ui.console_parts.streaming import StreamingRenderer

CommandOutputSink = Callable[[str], None]
CommandOutputWidth = int | Callable[[], int]

_command_output_sink: ContextVar[CommandOutputSink | None] = ContextVar(
    "command_output_sink",
    default=None,
)
_command_output_width: ContextVar[CommandOutputWidth] = ContextVar(
    "command_output_width",
    default=80,
)


def _capture_width() -> int:
    width = _command_output_width.get()
    if callable(width):
        try:
            return max(int(width()), 20)
        except Exception:
            return 80
    return max(int(width), 20)


class VoidConsole:
    """Thin wrapper with voidx-specific rendering primitives."""

    _TOOL_GERUND: dict[str, str] = {
        "read": "reading", "write": "writing", "edit": "editing",
        "glob": "finding", "grep": "searching", "bash": "running",
        "task": "running", "webfetch": "fetching", "websearch": "searching",
        "todo": "updating", "task_status": "checking", "repo_map": "mapping",
    }

    _AGENT_GERUND: dict[str, str] = {
        "orchestrator": "thinking",
        "explore": "exploring",
        "plan": "planning",
        "implement": "implementing",
        "review": "reviewing",
    }

    def __init__(self) -> None:
        self._console = Console()
        self._debug = True
        self._pending_tools: dict[str, list[dict[str, object]]] = {}
        self._event_tool_ids: dict[str, list[str]] = {}

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

    @contextmanager
    def capture_command_output(
        self,
        sink: CommandOutputSink,
        *,
        width: CommandOutputWidth = 80,
    ) -> Iterator[None]:
        sink_token = _command_output_sink.set(sink)
        width_token = _command_output_width.set(width)
        try:
            yield
        finally:
            _command_output_width.reset(width_token)
            _command_output_sink.reset(sink_token)

    def _emit_command_output(self, render: Callable[[Console], None]) -> bool:
        sink = _command_output_sink.get()
        if sink is None:
            return False
        text = _capture_ansi(_capture_width(), render)
        if text:
            sink(text)
        return True

    def print(self, *args, **kwargs) -> None:
        if self._emit_command_output(lambda console: console.print(*args, **kwargs)):
            return
        if dock.active and ui_events.is_running:
            text = _capture_ansi(
                self._console.width,
                lambda console: console.print(*args, **kwargs),
            )
            if text:
                ui_events.emit_nowait(AnsiAppended(text=text))
            return
        if dock.active and dock.print(*args, **kwargs):
            return
        self._console.print(*args, **kwargs)

    def markdown(self, content: str) -> None:
        if self._emit_command_output(lambda console: console.print(Markdown(content))):
            return
        if dock.active and ui_events.is_running:
            ui_events.emit_nowait(MarkdownAppended(content=content))
            return
        if dock.active and dock.capture(lambda console: console.print(Markdown(content))):
            return
        self._console.print(Markdown(content))

    def thinking(self, text: str) -> None:
        if self._emit_command_output(lambda console: console.print(Text(text, style="dim italic"))):
            return
        if dock.active and ui_events.is_running:
            captured = _capture_ansi(
                self._console.width,
                lambda console: console.print(Text(text, style="dim italic")),
            )
            if captured:
                ui_events.emit_nowait(AnsiAppended(text=captured))
            return
        if dock.active and dock.capture(lambda console: console.print(Text(text, style="dim italic"))):
            return
        self._console.print(Text(text, style="dim italic"))

    def tool_call(self, tool_name: str, args: dict[str, object]) -> None:
        if not self._debug:
            self._pending_tools.setdefault(tool_name, []).append(args)
            return
        gerund = _title(self._TOOL_GERUND.get(tool_name, tool_name + "ing"))
        if dock.active and ui_events.is_running:
            event_id = _event_tool_id(tool_name)
            self._event_tool_ids.setdefault(tool_name, []).append(event_id)
            ui_events.emit_nowait(ToolStarted(
                tool_call_id=event_id,
                tool_name=tool_name,
                label=gerund,
                args=_fmt_args(args),
            ))
            return
        if dock.active:
            dock.start_tool(gerund, _fmt_args(args))
            return
        self.print(f"  {_next_spin()} [bold]{gerund}[/bold]({_fmt_args(args)})")

    def tool_done(self, tool_name: str, elapsed: float, ok: bool = True) -> None:
        icon = _done_spin() if ok else "[red]●[/red]"
        style = "green" if ok else "red"
        label = _title(tool_name)
        if not self._debug:
            pending = self._pending_tools.get(tool_name, [])
            args = pending.pop(0) if pending else {}
            if not pending:
                self._pending_tools.pop(tool_name, None)
            elapsed_part = f" [dim]({elapsed:.1f}s)[/dim]" if elapsed >= 2 else ""
            if dock.active and ui_events.is_running:
                event_id = _event_tool_id(tool_name)
                detail = _fmt_args_short(tool_name, args)
                ui_events.emit_nowait(ToolStarted(
                    tool_call_id=event_id,
                    tool_name=tool_name,
                    label=label,
                    args=detail,
                ))
                ui_events.emit_nowait(ToolFinished(
                    tool_call_id=event_id,
                    label=label,
                    elapsed=elapsed,
                    ok=ok,
                ))
                return
            if dock.active:
                detail = _fmt_args_short(tool_name, args)
                dock.start_tool(label, detail)
                dock.finish_tool(label, elapsed, ok)
                return
            self.print(
                f"  {icon} [{style}]{label}[/] [dim]{_fmt_args_short(tool_name, args)}[/]{elapsed_part}"
            )
            return
        if dock.active and ui_events.is_running:
            event_id = _pop_event_tool_id(self._event_tool_ids, tool_name)
            if event_id:
                ui_events.emit_nowait(ToolFinished(
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

    def tool_result(self, text: str) -> None:
        if dock.active and ui_events.is_running:
            ui_events.emit_nowait(ToolResultAppended(text=text))
            return
        if dock.active:
            dock.append_tool_result(text)
            return
        self.print(text)

    def error(self, message: str) -> None:
        if self._emit_command_output(
            lambda console: console.print(Panel(message, border_style="red", title="error"))
        ):
            return
        if dock.active and ui_events.is_running:
            ui_events.emit_nowait(ErrorAppended(message=message))
            return
        if dock.active:
            dock.append_error(message)
            return
        self._console.print(Panel(message, border_style="red", title="error"))

    def warn(self, message: str) -> None:
        self.print(f"[yellow]! {message}[/yellow]")

    def sep(self) -> None:
        w = self._console.width or 80
        self.print("─" * w, style="dim")

    def step_header(self, n: int, max_n: int, agent: str = "") -> None:
        gerund = _title(self._AGENT_GERUND.get(agent, agent))
        label = f"Agent step {n}/{max_n}" if agent == "orchestrator" else f"{gerund} {n}/{max_n}"
        if dock.active and ui_events.is_running:
            ui_events.emit_nowait(StatusUpdated(
                status_id="agent:-1:progress",
                label=label,
                stage="agent_step",
            ))
            return
        if not self._debug:
            return
        if dock.active:
            return
        self.print(f"  {_next_spin()} [dim]{gerund} ({n}/{max_n})[/dim]")

    def diff(self, diff_text: str, title: str = "") -> None:
        from voidx.ui.diff import render_diff
        if self._emit_command_output(lambda console: render_diff(console, diff_text, title)):
            return
        if dock.active and ui_events.is_running:
            ui_events.emit_nowait(DiffAppended(diff_text=diff_text, title=title))
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

    def step_header(self, n: int, max_n: int, agent: str = "") -> None:
        gerund = _title(VoidConsole._AGENT_GERUND.get(agent, agent))
        self._console.print(f"  {_next_spin()} [dim]{gerund} ({n}/{max_n})[/dim]")

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
        from voidx.ui.diff import render_diff
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
