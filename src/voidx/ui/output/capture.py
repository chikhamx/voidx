from __future__ import annotations
import time

from rich.markup import escape
from rich.markdown import Markdown

from voidx.ui.output.agent_display import agent_display_name
from voidx.ui.output.tree import OutputTree, OutputNode
from voidx.ui.output.console import _fmt_args, _title, VoidConsole
from voidx.ui.output.dock import dock
from voidx.ui.output.dock.nodes import _bash_markdown_lines, _tool_header
from voidx.ui.output.events import (
    ErrorAppended,
    SubagentStepStarted,
    ToolFinished,
    ToolResultAppended,
    ToolStarted,
    WarningAppended,
    ui_events,
    via_events,
)

class _DummyConsole:
    width: int = 80

class CaptureConsole:
    def __init__(self, tree: OutputTree, parent_node: OutputNode, *, agent_id: int = -1):
        self._tree = tree
        self._parent = parent_node
        self._agent_id = agent_id
        self._current_tool: OutputNode | None = None
        self._current_tool_id: str = ""
        self._tool_nodes: dict[str, OutputNode] = {}
        self._dummy = _DummyConsole()

    @property
    def console(self) -> _DummyConsole:
        return self._dummy

    def step_header(self, agent: str = "") -> None:
        if via_events():
            ui_events.emit_direct(SubagentStepStarted(
                agent_id=self._agent_id,
                subagent_id=agent or "subagent",
                name=_title(VoidConsole._AGENT_GERUND.get(agent, agent)),
            ))
            return
        gerund = _title(VoidConsole._AGENT_GERUND.get(agent, agent))
        self._tree.new_node(
            parent=self._parent, node_type="turn",
            header=f"⟳ {gerund}",
            agent_name=agent if agent else None,
            collapsed=False,
        )
        dock.refresh()

    def tool_call(self, tool_name: str, args: dict[str, object], tool_call_id: str | None = None) -> None:
        gerund = _title(VoidConsole._TOOL_GERUND.get(tool_name, tool_name + "ing"))
        call_id = tool_call_id or f"capture:{tool_name}:{time.time_ns()}"
        self._current_tool_id = call_id
        if via_events():
            ui_events.emit_direct(ToolStarted(
                agent_id=self._agent_id,
                tool_call_id=call_id,
                tool_name=tool_name,
                label=gerund,
                args=_fmt_args(args),
                raw_args=args,
            ))
            return
        body_lines = []
        detail = f"({_fmt_args(args)})"
        if tool_name == "agent":
            detail = ""
            gerund = agent_display_name(args.get("name"))
        elif tool_name in ("bash", "powershell"):
            command = str(args.get("command") or "")
            detail = ""
            body_lines = _bash_markdown_lines(command, self._dummy.width)
        elif tool_name.startswith("mcp__"):
            gerund = _tool_header(tool_name, gerund, _fmt_args(args), args)
            detail = ""
        self._current_tool = self._tree.new_node(
            parent=self._parent, node_type="tool_call",
            header=f"● {gerund}{detail}",
            body_lines=body_lines,
            status="running", collapsed=True,
        )
        self._tool_nodes[call_id] = self._current_tool
        dock.refresh()
    
    def tool_done(self, tool_name: str, elapsed: float, ok: bool = True, tool_call_id: str | None = None) -> None:
        call_id = tool_call_id or self._current_tool_id
        if via_events() and call_id:
            ui_events.emit_direct(ToolFinished(
                agent_id=self._agent_id,
                tool_call_id=call_id,
                label=_title(tool_name),
                elapsed=elapsed,
                ok=ok,
            ))
            return
        tool_node = self._tool_nodes.get(call_id) or self._current_tool
        if not tool_node: return
        icon = "●" if ok else "✗"
        tool_node.header += f"  {icon} {_title(tool_name)} ({elapsed:.1f}s)"
        tool_node.elapsed = elapsed
        tool_node.status = "done" if ok else "error"
        self._tree.mark_dirty()
        dock.refresh()
    
    def tool_result(self, text: str, tool_call_id: str | None = None) -> None:
        call_id = tool_call_id or self._current_tool_id
        if via_events():
            ui_events.emit_direct(ToolResultAppended(
                agent_id=self._agent_id,
                tool_call_id=call_id,
                text=text,
            ))
            return
        parent = self._tool_nodes.get(call_id) or self._current_tool or self._parent
        lines = text.split("\n")
        self._tree.new_node(
            parent=parent, node_type="tool_result",
            header=escape(lines[0]) if lines else "",
            body_lines=[escape(line) for line in lines[1:]], collapsed=False,
        )
        dock.refresh()
    
    def diff(self, diff_text: str, title: str = "") -> None:
        if via_events():
            text = f"{title}\n{diff_text}" if title else diff_text
            ui_events.emit_direct(ToolResultAppended(
                agent_id=self._agent_id,
                tool_call_id=self._current_tool_id,
                text=text,
                collapsed=True,
            ))
            return
        parent = self._current_tool or self._parent
        lines = diff_text.split("\n")
        self._tree.new_node(
            parent=parent, node_type="diff",
            header=escape((title or "diff")[:80]),
            body_lines=[escape(line) for line in lines[:20]], collapsed=True,
        )
        dock.refresh()
    
    def print(self, *args, **kwargs) -> None:
        if via_events():
            return
        dock.capture(lambda console: console.print(*args, **kwargs), parent=self._parent)
    
    def markdown(self, content: str) -> None:
        if via_events():
            return
        dock.capture(lambda console: console.print(Markdown(content)), parent=self._parent)
    
    def thinking(self, text: str) -> None:
        if via_events():
            return
        dock.append_thought(text, parent=self._parent)
    
    def error(self, message: str) -> None:
        if via_events():
            ui_events.emit_direct(ErrorAppended(agent_id=self._agent_id, message=message))
            return
        self._tree.new_node(parent=self._parent, node_type="error",
            header=f"[red]✗ {escape(message)}[/]", header_style="red", collapsed=False)
        dock.refresh()
    
    def warn(self, message: str) -> None:
        if via_events():
            ui_events.emit_direct(WarningAppended(agent_id=self._agent_id, message=message))
            return
        self._tree.new_node(parent=self._parent, node_type="warn",
            header=f"[yellow]! {escape(message)}[/]", header_style="yellow", collapsed=False)
        dock.refresh()
    
    def sep(self) -> None:
        if via_events():
            return
        dock.append_message("─" * self._dummy.width, style="dim", parent=self._parent)
