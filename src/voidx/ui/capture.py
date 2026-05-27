from __future__ import annotations
from voidx.ui.tree import OutputTree, OutputNode
from voidx.ui.console import _fmt_args, VoidConsole

class _DummyConsole:
    width: int = 80

class CaptureConsole:
    def __init__(self, tree: OutputTree, parent_node: OutputNode):
        self._tree = tree
        self._parent = parent_node
        self._current_tool: OutputNode | None = None
        self._dummy = _DummyConsole()

    @property
    def console(self) -> _DummyConsole:
        return self._dummy

    def step_header(self, n: int, max_n: int, agent: str = "") -> None:
        gerund = VoidConsole._AGENT_GERUND.get(agent, agent)
        self._tree.new_node(
            parent=self._parent, node_type="turn",
            header=f"⟳ {gerund} ({n}/{max_n})",
            step_info=f"step {n}/{max_n}",
            agent_name=agent if agent else None,
            collapsed=False,
        )

    def tool_call(self, tool_name: str, args: dict[str, object]) -> None:
        gerund = VoidConsole._TOOL_GERUND.get(tool_name, tool_name + "ing")
        self._current_tool = self._tree.new_node(
            parent=self._parent, node_type="tool_call",
            header=f"● {gerund}({_fmt_args(args)})",
            status="running", collapsed=True,
        )
    
    def tool_done(self, tool_name: str, elapsed: float, ok: bool = True) -> None:
        if not self._current_tool: return
        icon = "✓" if ok else "✗"
        self._current_tool.header += f"  {icon} ({elapsed:.1f}s)"
        self._current_tool.elapsed = elapsed
        self._current_tool.status = "done" if ok else "error"
    
    def tool_result(self, text: str) -> None:
        parent = self._current_tool or self._parent
        lines = text[:600].split("\n")
        self._tree.new_node(
            parent=parent, node_type="tool_result",
            header=lines[0][:100] if lines else "",
            body_lines=lines, collapsed=True,
        )
    
    def diff(self, diff_text: str, title: str = "") -> None:
        parent = self._current_tool or self._parent
        lines = diff_text.split("\n")
        self._tree.new_node(
            parent=parent, node_type="diff",
            header=(title or "diff")[:80],
            body_lines=lines[:20], collapsed=True,
        )
    
    def print(self, *args, **kwargs) -> None:
        pass
    
    def markdown(self, content: str) -> None:
        pass
    
    def thinking(self, text: str) -> None:
        pass
    
    def error(self, message: str) -> None:
        self._tree.new_node(parent=self._parent, node_type="error",
            header=f"✗ {message}", header_style="red", collapsed=False)
    
    def warn(self, message: str) -> None:
        self._tree.new_node(parent=self._parent, node_type="warn",
            header=f"! {message}", header_style="yellow", collapsed=False)
    
    def sep(self) -> None:
        pass
