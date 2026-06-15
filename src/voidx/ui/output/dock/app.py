"""Rich Live/Layout backed bottom input dock."""

from __future__ import annotations

from io import StringIO
from typing import Any, Callable, Sequence

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.text import Text

from voidx.ui.output.dock.formatting import (
    ANSI_LINE_PREFIX,
    _clean,
    _strip_ansi_trailing_space,
    _text_from_line,
)
from voidx.ui.output.dock.nodes import DockNodeMixin
from voidx.ui.output.dock.stream import DockStreamMixin
from voidx.ui.output.dock.state import dock, get_dock, set_dock
from voidx.ui.output.dock.todo import (
    DockTodoState,
    render_todo_header,
    render_todo_state_lines,
    todo_state_payload,
    todo_state_from_items,
)
from voidx.ui.output.dock.agent_placeholder import agent_placeholder_header
from voidx.ui.output.tree import OutputNode, OutputTree


from voidx.ui.output.dock.status import DockStatusMixin, DockStatusRecord, active_agent_step_text
class BottomInputDock(DockStreamMixin, DockStatusMixin, DockNodeMixin):
    """Render agent output above a fixed input box with Rich Live/Layout."""

    def __init__(self) -> None:
        self._console = Console()
        self._active = False
        self._live: Live | None = None
        self._stopping = False
        self._tree = OutputTree()
        self._current_turn: OutputNode | None = None
        self._current_agent: OutputNode | None = None
        self._current_tool: OutputNode | None = None
        self._stream_node: OutputNode | None = None
        self._stream_text = ""
        self._status_nodes: dict[str, OutputNode] = {}
        self._status_ticks: dict[str, int] = {}
        self._status_records: dict[str, DockStatusRecord] = {}
        self._todo_state: DockTodoState | None = None
        self._permission_node: OutputNode | None = None
        self._settled_node_ids: set[str] = set()
        self._input_text = ""
        self._cursor_pos = 0
        self._hints: list[tuple[str, str, bool]] = []
        self._refresh_callback: Callable[[], None] | None = None
        self._width_provider: Callable[[], int] | None = None
        self._needs_clear_screen: bool = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def tree(self) -> OutputTree:
        return self._tree

    @property
    def current_turn(self) -> OutputNode | None:
        return self._current_turn

    @property
    def current_agent(self) -> OutputNode | None:
        return self._current_agent

    def set_todo_state(self, summary: str, items: Sequence[Any]) -> None:
        self._todo_state = todo_state_from_items(summary, items)
        self.refresh()

    def clear_todo_state(self) -> None:
        if self._todo_state is None:
            return
        self._todo_state = None
        self.refresh()

    def todo_state(self) -> DockTodoState | None:
        return self._todo_state

    def commit_todo_state(self) -> OutputNode | None:
        state = self._todo_state
        if state is None:
            return None
        node = self._tree.new_node(
            parent=self._tree.root,
            node_type="todo",
            header=render_todo_header(state),
            body_lines=render_todo_state_lines(state),
            collapsed=False,
            status="done",
            payload=todo_state_payload(state),
        )
        self._mark_settled(node)
        self._todo_state = None
        self.refresh()
        return node

    def set_refresh_callback(self, callback: Callable[[], None] | None) -> None:
        self._refresh_callback = callback

    def set_width_provider(self, callback: Callable[[], int] | None) -> None:
        self._width_provider = callback

    def consume_clear_screen_request(self) -> bool:
        requested = self._needs_clear_screen
        self._needs_clear_screen = False
        return requested

    def activate(self) -> None:
        if self._live:
            self.refresh()
            return
        self._active = True
        self._stopping = False
        self._stream_text = ""
        self._live = Live(
            console=self._console,
            auto_refresh=False,
            refresh_per_second=20,
            transient=True,
            redirect_stdout=False,
            redirect_stderr=False,
            get_renderable=self._render,
        )
        self._live.start(refresh=True)

    def begin_capture(self) -> None:
        if self._active:
            return
        self._active = True
        self._stream_text = ""

    def deactivate(self) -> None:
        if not self._active:
            return
        self._stream_node = None
        self._stream_text = ""
        if self._live:
            self._stopping = True
            self._live.stop()
            self._live = None
            self._stopping = False
        self._active = False

    def reset(self) -> None:
        self._tree = OutputTree()
        self._settled_node_ids.clear()
        self._todo_state = None
        self._reset_runtime_nodes()
        self._input_text = ""
        self._cursor_pos = 0
        self._hints = []
        self._needs_clear_screen = True
        self.refresh()

    def restore_tree(self, tree: OutputTree, *, append: bool = False) -> None:
        if append:
            self._tree.extend_from(tree)
        else:
            self._tree = tree
        self._settled_node_ids.clear()
        self._mark_tree_settled()
        self._reset_runtime_nodes()
        self._todo_state = None
        self.refresh()

    def _reset_runtime_nodes(self) -> None:
        self._current_turn = None
        self._current_agent = None
        self._current_tool = None
        self._stream_node = None
        self._stream_text = ""
        self._status_nodes = {}
        self._status_ticks = {}
        self._status_records = {}
        self._permission_node = None

    def start_turn(self, text: str) -> OutputNode:
        self.commit_stream()
        self._current_tool = None
        self._current_agent = None
        if self._tree.root.children:
            self._append_root_spacer()
        preview = _clean(text)
        lines = [_strip_ansi_trailing_space(line) for line in (preview.splitlines() or [preview])]
        self._current_turn = self._tree.new_node(
            parent=self._tree.root,
            node_type="turn",
            header=f"[bold white]❯[/] {escape(lines[0])}",
            body_lines=[escape(line) for line in lines[1:]],
            collapsed=False,
        )
        self._mark_settled(self._current_turn)
        self.refresh()
        return self._current_turn

    def ensure_agent(self) -> OutputNode:
        if self._current_agent is None:
            self._append_root_spacer()
            self._current_agent = self._tree.new_node(
                parent=self._tree.root,
                node_type="assistant",
                header=agent_placeholder_header(),
                collapsed=False,
            )
            self._mark_unsettled(self._current_agent)
            self.refresh()
        return self._current_agent

    def print(self, *args, parent: OutputNode | None = None, **kwargs) -> bool:
        if not self._active:
            return False
        self.capture(lambda console: console.print(*args, **kwargs), parent=parent)
        return True

    def capture(
        self,
        render: Callable[[Console], None],
        *,
        parent: OutputNode | None = None,
    ) -> bool:
        if not self._active:
            return False
        buffer = StringIO()
        console = Console(
            file=buffer,
            force_terminal=True,
            color_system="truecolor",
            width=self._width(),
        )
        render(console)
        text = buffer.getvalue().rstrip("\n")
        if text:
            self.append_ansi(text, parent=parent)
        return True

    def set_input(
        self,
        text: str,
        hints: list[tuple[str, str, bool]] | None = None,
        cursor_pos: int | None = None,
    ) -> None:
        self._input_text = text
        self._cursor_pos = max(0, min(len(text), len(text) if cursor_pos is None else cursor_pos))
        self._hints = hints or []
        self.refresh()

    def after_output(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        if self._refresh_callback:
            self._refresh_callback()
        if self._live:
            self._live.update(self._render(), refresh=True)

    def _render(self) -> Group:
        if self._stopping:
            return Group(Text(""))

        width = max(self._width() - 1, 3)
        hint_lines = []
        for name, desc, selected in self._hints[:6]:
            style = "bold blue" if selected else "dim"
            hint_lines.append(Text.assemble(("  " + name, style), ("  " + desc, style)))

        input_height = 3 + len(hint_lines)
        body_limit = max((self._console.height or 24) - input_height - 1, 1)
        lines = self._tree.render(self._width())

        startup_count = self._tree.startup_line_count()
        startup_lines = lines[:startup_count] if startup_count > 0 else []
        remaining_limit = max(body_limit - startup_count, 1)
        scrollable_lines = lines[startup_count:]
        visible = startup_lines + scrollable_lines[-remaining_limit:]
        body = Group(*[_text_from_line(line) for line in visible]) if visible else Text("")

        border = "─" * width
        input_box = Text.assemble(
            (border + "\n", "white"),
            ("❯ ", "bold white"),
            *self._render_input_text(),
            ("\n" + border, "white"),
        )
        input_renderable = Group(input_box, *hint_lines)
        return Group(body, input_renderable)

    def _render_input_text(self) -> list[tuple[str, str]]:
        text = self._input_text
        cursor = self._cursor_pos
        before = text[:cursor]
        at = text[cursor:cursor + 1]
        after = text[cursor + 1:]
        parts: list[tuple[str, str]] = []
        if before:
            parts.append((before, "white"))
        if at and at != "\n":
            parts.append((at, "reverse white"))
        else:
            parts.append((" ", "reverse white"))
            if at == "\n":
                parts.append(("\n", "white"))
        if after:
            parts.append((after, "white"))
        return parts

    def _mark_settled(self, node: OutputNode | None) -> None:
        if node is not None:
            self._settled_node_ids.add(node.id)

    def _mark_unsettled(self, node: OutputNode | None) -> None:
        if node is not None:
            self._settled_node_ids.discard(node.id)

    def _mark_subtree_settled(self, node: OutputNode | None) -> None:
        if node is None:
            return
        self._settled_node_ids.add(node.id)
        for child in node.children:
            self._mark_subtree_settled(child)

    def _mark_tree_settled(self) -> None:
        for child in self._tree.root.children:
            self._mark_subtree_settled(child)

    def _discard_settled_subtree(self, node: OutputNode | None) -> None:
        if node is None:
            return
        self._settled_node_ids.discard(node.id)
        for child in node.children:
            self._discard_settled_subtree(child)

    def _is_node_chain_settled(self, node_id: str) -> bool:
        node = self._tree.get(node_id)
        while node is not None and node is not self._tree.root:
            if node.id not in self._settled_node_ids:
                return False
            node = node.parent
        return True

    def safe_flush_line_count(self, width: int, committed: int) -> int:
        lines, line_map = self._tree.render_with_line_map(width)
        index = max(0, min(committed, len(lines)))
        while index < len(lines):
            node_id = line_map.get(index)
            if node_id is None:
                if lines[index].strip():
                    break
                index += 1
                continue
            if not self._is_node_chain_settled(node_id):
                break
            index += 1
        return index

    def mark_node_settled(self, node: OutputNode | None) -> None:
        self._mark_subtree_settled(node)

    def mark_node_unsettled(self, node: OutputNode | None) -> None:
        self._mark_unsettled(node)

    def _append_root_spacer(self) -> None:
        children = self._tree.root.children
        if not children:
            return
        last = children[-1]
        if last.node_type == "message" and not last.header and not last.body_lines and not last.children:
            return
        node = self._tree.new_node(
            parent=self._tree.root,
            node_type="message",
            header="",
            collapsed=False,
        )
        self._mark_settled(node)

    def _remove_node(self, node: OutputNode) -> None:
        self._discard_settled_subtree(node)
        parent = node.parent
        if parent and node in parent.children:
            parent.children.remove(node)
            for index, child in enumerate(parent.children):
                child._is_last_sibling = index == len(parent.children) - 1
        for child in list(node.children):
            self._remove_node(child)
        self._tree._all.pop(node.id, None)
        self._tree.mark_dirty()

    def _width(self) -> int:
        if self._width_provider is not None:
            return max(self._width_provider(), 20)
        return max(self._console.width or 80, 20)

    def _markdown_width(self) -> int:
        return max(self._width() - 4, 20)
