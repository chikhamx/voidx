from __future__ import annotations
import time

from rich.markup import escape

from voidx.ui.tree import OutputTree, OutputNode
from voidx.ui.console import _fmt_args, _title, VoidConsole
from voidx.ui.dock import dock
from voidx.ui.events import (
    ErrorAppended,
    SubagentStepStarted,
    ToolFinished,
    ToolResultAppended,
    ToolStarted,
    WarningAppended,
    ui_events,
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
        self._dummy = _DummyConsole()

    @property
    def console(self) -> _DummyConsole:
        return self._dummy

    def step_header(self, n: int, max_n: int, agent: str = "") -> None:
        if dock.active and ui_events.is_running:
            ui_events.emit_nowait(SubagentStepStarted(
                agent_id=self._agent_id,
                subagent_id=agent or "subagent",
                name=_title(VoidConsole._AGENT_GERUND.get(agent, agent)),
                step=n,
                max_steps=max_n,
            ))
            return
        gerund = _title(VoidConsole._AGENT_GERUND.get(agent, agent))
        self._tree.new_node(
            parent=self._parent, node_type="turn",
            header=f"⟳ {gerund} ({n}/{max_n})",
            step_info=f"step {n}/{max_n}",
            agent_name=agent if agent else None,
            collapsed=False,
        )
        dock.refresh()

    def tool_call(self, tool_name: str, args: dict[str, object]) -> None:
        gerund = _title(VoidConsole._TOOL_GERUND.get(tool_name, tool_name + "ing"))
        if dock.active and ui_events.is_running:
            self._current_tool_id = f"capture:{tool_name}:{time.time_ns()}"
            ui_events.emit_nowait(ToolStarted(
                agent_id=self._agent_id,
                tool_call_id=self._current_tool_id,
                tool_name=tool_name,
                label=gerund,
                args=_fmt_args(args),
            ))
            return
        self._current_tool = self._tree.new_node(
            parent=self._parent, node_type="tool_call",
            header=f"● {gerund}({_fmt_args(args)})",
            status="running", collapsed=True,
        )
        dock.refresh()
    
    def tool_done(self, tool_name: str, elapsed: float, ok: bool = True) -> None:
        if dock.active and ui_events.is_running and self._current_tool_id:
            ui_events.emit_nowait(ToolFinished(
                agent_id=self._agent_id,
                tool_call_id=self._current_tool_id,
                label=_title(tool_name),
                elapsed=elapsed,
                ok=ok,
            ))
            return
        if not self._current_tool: return
        icon = "●" if ok else "✗"
        self._current_tool.header += f"  {icon} {_title(tool_name)} ({elapsed:.1f}s)"
        self._current_tool.elapsed = elapsed
        self._current_tool.status = "done" if ok else "error"
        dock.refresh()
    
    def tool_result(self, text: str) -> None:
        if dock.active and ui_events.is_running:
            ui_events.emit_nowait(ToolResultAppended(
                agent_id=self._agent_id,
                tool_call_id=self._current_tool_id,
                text=text,
            ))
            return
        parent = self._current_tool or self._parent
        lines = text.split("\n")
        self._tree.new_node(
            parent=parent, node_type="tool_result",
            header=escape(lines[0]) if lines else "",
            body_lines=[escape(line) for line in lines[1:]], collapsed=True,
        )
        dock.refresh()
    
    def diff(self, diff_text: str, title: str = "") -> None:
        if dock.active and ui_events.is_running:
            text = f"{title}\n{diff_text}" if title else diff_text
            ui_events.emit_nowait(ToolResultAppended(
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
        pass
    
    def markdown(self, content: str) -> None:
        pass
    
    def thinking(self, text: str) -> None:
        pass
    
    def error(self, message: str) -> None:
        if dock.active and ui_events.is_running:
            ui_events.emit_nowait(ErrorAppended(agent_id=self._agent_id, message=message))
            return
        self._tree.new_node(parent=self._parent, node_type="error",
            header=f"[red]✗ {escape(message)}[/]", header_style="red", collapsed=False)
        dock.refresh()
    
    def warn(self, message: str) -> None:
        if dock.active and ui_events.is_running:
            ui_events.emit_nowait(WarningAppended(agent_id=self._agent_id, message=message))
            return
        self._tree.new_node(parent=self._parent, node_type="warn",
            header=f"[yellow]! {escape(message)}[/]", header_style="yellow", collapsed=False)
        dock.refresh()
    
    def sep(self) -> None:
        pass
