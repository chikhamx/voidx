"""Rich console — smooth streaming, status indicators, Claude Code style."""

from __future__ import annotations

import time
from types import TracebackType

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text


class VoidConsole:
    """Thin wrapper with voidx-specific rendering primitives."""

    # Gerund forms for tool execution progress
    _TOOL_GERUND: dict[str, str] = {
        "read": "reading", "write": "writing", "edit": "editing",
        "glob": "finding", "grep": "searching", "bash": "running",
        "task": "running", "webfetch": "fetching", "websearch": "searching",
        "todo": "updating", "task_status": "checking", "repo_map": "mapping",
    }

    # Gerund forms for agent states
    _AGENT_GERUND: dict[str, str] = {
        "orchestrator": "thinking",
        "explore": "exploring",
        "plan": "planning",
        "implement": "implementing",
        "review": "reviewing",
    }

    def __init__(self) -> None:
        self._console = Console()

    @property
    def console(self) -> Console:
        return self._console

    def print(self, *args, **kwargs) -> None:
        self._console.print(*args, **kwargs)

    def markdown(self, content: str) -> None:
        self._console.print(Markdown(content))

    def thinking(self, text: str) -> None:
        self._console.print(Text(text, style="dim italic"))

    def tool_call(self, tool_name: str, args: dict[str, object]) -> None:
        gerund = self._TOOL_GERUND.get(tool_name, tool_name + "ing")
        self._console.print(
            f"  [yellow]●[/yellow] [bold]{gerund}[/bold]({_fmt_args(args)})"
        )

    def tool_done(self, tool_name: str, elapsed: float, ok: bool = True) -> None:
        icon = "✓" if ok else "✗"
        style = "green" if ok else "red"
        self._console.print(
            f"  [{style}]{icon} {tool_name}[/{style}] [dim]({elapsed:.1f}s)[/dim]"
        )

    def tool_result(self, text: str) -> None:
        out = text[:600]
        if len(text) > 600:
            out += f"\n[dim]... (+{len(text) - 600} chars)[/dim]"
        self._console.print(out)

    def error(self, message: str) -> None:
        self._console.print(Panel(message, border_style="red", title="error"))

    def warn(self, message: str) -> None:
        self._console.print(f"[yellow]! {message}[/yellow]")

    def sep(self) -> None:
        w = self._console.width or 80
        self._console.print("─" * w, style="dim")

    def step_header(self, n: int, max_n: int, agent: str = "") -> None:
        gerund = self._AGENT_GERUND.get(agent, agent)
        self._console.print(f"  [dim]⟳ {gerund} ({n}/{max_n})[/dim]")

    def diff(self, diff_text: str, title: str = "") -> None:
        from voidx.ui.diff import render_diff
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
        gerund = VoidConsole._AGENT_GERUND.get(agent, agent)
        self._console.print(f"  [dim]{gerund} ({n}/{max_n})[/dim]")

    def tool_call(self, tool_name: str, args: dict[str, object]) -> None:
        gerund = VoidConsole._TOOL_GERUND.get(tool_name, tool_name + "ing")
        self._console.print(
            f"  [yellow]●[/yellow] [bold]{gerund}[/bold]({fmt_args(args)})"
        )
        self._current_tool = self._tree.new_node(
            parent=self._turn,
            node_type="tool_call",
            header=f"[yellow]●[/yellow] [bold]{gerund}[/bold]({fmt_args(args)})",
            status="running",
        )

    def tool_done(self, tool_name: str, elapsed: float, ok: bool = True) -> None:
        icon = "OK" if ok else "FAIL"
        style = "green" if ok else "red"
        self._console.print(
            f"  [{style}]{icon} {tool_name}[/{style}] [dim]({elapsed:.1f}s)[/dim]"
        )
        if self._current_tool:
            self._current_tool.header += f"  [{style}]{icon} ({elapsed:.1f}s)[/{style}]"
            self._current_tool.elapsed = elapsed
            self._current_tool.status = "done" if ok else "error"

    def tool_result(self, text: str) -> None:
        out = text[:600]
        if len(text) > 600:
            out += f"\n[dim]... (+{len(text) - 600} chars)[/dim]"
        self._console.print(out)
        if self._current_tool:
            preview = text[:600]
            if len(text) > 600:
                preview += f"\n[dim]... (+{len(text) - 600} chars)[/dim]"
            self._tree.new_node(
                parent=self._current_tool,
                node_type="tool_result",
                body_lines=preview.split("\n"),
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


class StreamingRenderer:
    """Smooth streaming with Rich Live + Markdown rendering."""

    FLUSH_INTERVAL = 0.05
    _spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, console: Console) -> None:
        self._console = console
        self._thinking: list[str] = []
        self._accumulated: str = ""
        self._phase: str = "thinking"
        self._last_flush: float = 0.0
        self._live: Live | None = None
        self._start_time: float = time.monotonic()
        self._first_text: bool = True
        self._spinner_idx: int = 0

    async def __aenter__(self):
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None,
        exc_val: BaseException | None, exc_tb: TracebackType | None,
    ) -> None:
        self.done()

    def _next_spinner(self) -> str:
        f = self._spinner_frames[self._spinner_idx % len(self._spinner_frames)]
        self._spinner_idx += 1
        return f

    def feed_thinking(self, text: str) -> None:
        self._thinking.append(text)

    def feed_text(self, text: str) -> None:
        if self._thinking and self._phase == "thinking":
            self._flush_thinking()

        self._phase = "text"

        if self._first_text:
            self._first_text = False
            text = "● " + text.lstrip()

        self._accumulated += text

        if self._live is None:
            self._live = Live(
                Markdown(""), console=self._console,
                refresh_per_second=20, transient=False,
            )
            self._live.start()

        now = time.monotonic()
        if now - self._last_flush >= self.FLUSH_INTERVAL:
            self._live.update(Markdown(self._accumulated))
            self._last_flush = now

    def elapsed(self) -> float:
        return time.monotonic() - self._start_time

    def done(self) -> str:
        if self._thinking and self._phase == "thinking":
            self._flush_thinking()

        if self._live:
            if self._accumulated:
                self._live.update(Markdown(self._accumulated))
            self._live.stop()
            self._live = None

        full = self._accumulated
        if full.startswith("● "):
            full = full[2:]
        self._accumulated = ""
        self._thinking = []
        self._first_text = True
        return full

    def get_thinking_text(self) -> str:
        return "".join(self._thinking)

    def get_body_text(self) -> str:
        return self._accumulated

    THINKING_MAX_LINES = 5

    def _flush_thinking(self) -> None:
        thinking_text = "".join(self._thinking)
        if thinking_text.strip():
            lines = thinking_text.split("\n")
            total = len(lines)
            if total > self.THINKING_MAX_LINES:
                skipped = total - self.THINKING_MAX_LINES
                thinking_text = "\n".join(lines[-self.THINKING_MAX_LINES:])
                self._console.print(
                    Text(f"  {self._next_spinner()} Thinking… ", style="dim"),
                    end="",
                )
                self._console.print(Text(f"[{skipped} earlier lines folded]\n", style="dim"))
            else:
                self._console.print(
                    Text(f"  {self._next_spinner()} Thinking... ", style="dim"),
                    end="",
                )
            self._console.print(Text(thinking_text, style="dim italic"))
        self._thinking = []


def _fmt_args(args: dict[str, object]) -> str:
    """Format tool args Claude Code style: key="value" inside parentheses."""
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 60:
            s = s[:57] + "..."
        if isinstance(v, str):
            parts.append(f'{k}="[cyan]{s}[/cyan]"')
        else:
            parts.append(f"{k}=[cyan]{s}[/cyan]")
    return ", ".join(parts)


fmt_args = _fmt_args
