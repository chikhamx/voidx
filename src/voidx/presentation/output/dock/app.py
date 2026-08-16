"""Rich Live/Layout backed bottom input dock."""

from __future__ import annotations

from io import StringIO
from typing import Any, Callable, Sequence

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from rich.markup import escape

from voidx.agent.domain.turn_metadata import TurnMetadata
from voidx.presentation.output.dock.formatting import (
    ANSI_LINE_PREFIX,
    _clean,
    strip_pasted_wrapper,
    text_from_line,
)
from voidx.presentation.output.dock.nodes import DockNodeMixin
from voidx.presentation.output.dock.stream import DockStreamMixin
from voidx.presentation.output.dock.state import dock, get_dock, set_dock
from voidx.presentation.output.dock.todo import (
    DockTodoState,
    render_todo_header,
    render_todo_state_lines,
    todo_state_payload,
    todo_state_from_items,
)
from voidx.presentation.output.dock.status import (
    DockStatusMixin,
    DockStatusRecord,
    active_permission_request_detail_text,
    active_permission_request_text,
    active_agent_step_text,
    active_compaction_detail_text,
    active_compaction_text,
    active_error_detail_text,
    active_error_text,
    active_guidance_preview_text,
    active_llm_retry_detail_text,
    active_llm_retry_text,
    active_turn_analyzing_text,
)
from voidx.presentation.output.tree import OutputNode, OutputTree, is_transparent_container


class BottomInputDock(DockStreamMixin, DockStatusMixin, DockNodeMixin):
    """Render agent output above a fixed input box with Rich Live/Layout."""

    def __init__(self) -> None:
        self._console = Console()
        self._active = False
        self._live: Live | None = None
        self._stopping = False
        self._tree = OutputTree()
        self._current_turn: OutputNode | None = None
        self._current_turn_text = ""
        self._current_turn_metadata = TurnMetadata()
        self._turn_in_progress = False
        self._current_agent: OutputNode | None = None
        self._current_tool: OutputNode | None = None
        self._stream_node: OutputNode | None = None
        self._stream_text = ""
        self._stream_thinking_text = ""
        self._last_committed_stream_text = ""
        self._last_committed_stream_parent_id: str | None = None
        self._last_committed_stream_node_id: str | None = None
        self._ignored_duplicate_stream_commit = False
        self._status_nodes: dict[str, OutputNode] = {}
        self._status_ticks: dict[str, int] = {}
        self._status_records: dict[str, DockStatusRecord] = {}
        self._todo_state: DockTodoState | None = None
        self._permission_node: OutputNode | None = None
        self._checkpoint_nodes: dict[str, OutputNode] = {}
        self._clarify_nodes: dict[str, OutputNode] = {}
        self._goal_spec_nodes: dict[str, OutputNode] = {}
        self._guidance_preview: str = ""
        self._guidance_echoes: list[str] = []
        self._settled_node_ids: set[str] = set()
        self._input_text = ""
        self._cursor_pos = 0
        self._hints: list[tuple[str, str, bool]] = []
        self._refresh_callback: Callable[[], None] | None = None
        self._width_provider: Callable[[], int] | None = None
        self._needs_clear_screen: bool = False
        self._needs_force_flush: bool = False
        self._restored_root_child_start: int | None = None
        self._restored_root_child_end: int | None = None

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
    def turn_in_progress(self) -> bool:
        return self._turn_in_progress

    @property
    def current_turn_text(self) -> str:
        return self._current_turn_text

    @property
    def current_turn_metadata(self) -> TurnMetadata:
        return self._current_turn_metadata

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

    def consume_force_flush_request(self) -> bool:
        requested = self._needs_force_flush
        self._needs_force_flush = False
        return requested

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
        self._stream_thinking_text = ""
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
        self._stream_thinking_text = ""

    def deactivate(self) -> None:
        if not self._active:
            return
        self._stream_node = None
        self._stream_text = ""
        self._stream_thinking_text = ""
        self._last_committed_stream_text = ""
        self._last_committed_stream_parent_id = None
        self._last_committed_stream_node_id = None
        self._ignored_duplicate_stream_commit = False
        if self._live:
            self._stopping = True
            self._live.stop()
            self._live = None
            self._stopping = False
        self._active = False

    def reset(self) -> None:
        self._tree = OutputTree()
        self._settled_node_ids.clear()
        self._guidance_echoes.clear()
        self._todo_state = None
        self._reset_runtime_nodes()
        self._input_text = ""
        self._cursor_pos = 0
        self._hints = []
        self._needs_clear_screen = True
        self._needs_force_flush = True
        self._restored_root_child_start = None
        self._restored_root_child_end = None
        self.refresh()

    def restore_tree(self, tree: OutputTree, *, append: bool = False) -> None:
        history_start = len(self._tree.root.children) if append else 0
        if append:
            self._tree.extend_from(tree)
        else:
            self._tree = tree
        self._settled_node_ids.clear()
        self._mark_tree_settled()
        self._reset_runtime_nodes()
        self._todo_state = None
        self._restored_root_child_start = history_start
        self._restored_root_child_end = len(self._tree.root.children)
        self.refresh()

    def restored_root_child_range(self) -> tuple[int, int] | None:
        if self._restored_root_child_start is None or self._restored_root_child_end is None:
            return None
        return self._restored_root_child_start, self._restored_root_child_end

    def _reset_runtime_nodes(self) -> None:
        self._current_turn = None
        self._current_turn_text = ""
        self._current_turn_metadata = TurnMetadata()
        self._turn_in_progress = False
        self._current_agent = None
        self._current_tool = None
        self._stream_node = None
        self._stream_text = ""
        self._stream_thinking_text = ""
        self._status_nodes = {}
        self._status_ticks = {}
        self._status_records = {}
        self._permission_node = None
        self._checkpoint_nodes = {}
        self._clarify_nodes = {}
        self._goal_spec_nodes = {}

    def start_turn(self, text: str, *, metadata: TurnMetadata | None = None) -> OutputNode:
        self.commit_stream()
        self._current_tool = None
        self._turn_in_progress = True
        self._current_turn_text = text
        self._current_turn_metadata = metadata or TurnMetadata()
        self._current_agent = None
        if self._tree.root.children:
            self._append_root_spacer()
        preview = _clean(text)
        preview = strip_pasted_wrapper(preview)
        header, body_lines = self._render_turn_text(preview)
        header = f"[bold white]❯[/] {header}" if header else "[bold white]❯[/]"
        self._current_turn = self._tree.new_node(
            parent=self._tree.root,
            node_type="turn",
            header=header,
            body_lines=body_lines,
            collapsed=False,
        )
        self._mark_settled(self._current_turn)
        self.refresh()
        return self._current_turn

    def end_turn(self) -> None:
        self._turn_in_progress = False
        self._current_turn_text = ""
        self._current_turn_metadata = TurnMetadata()
        self.refresh()

    def _render_turn_text(self, text: str) -> tuple[str, list[str]]:
        """Render user turn text into (header, body_lines) as plain text.

        <pasted> wrapper tags are stripped (content kept), then the entire
        text is rendered as escaped plain text — no markdown formatting.
        """
        lines = text.splitlines() or [text] if text else []
        if not lines:
            return "", []
        header = escape(lines[0])
        body_lines = [escape(line) for line in lines[1:]]
        return header, body_lines

    def set_guidance_preview(self, text: str) -> None:
        self._guidance_preview = text
        self.refresh()

    def clear_guidance_preview(self) -> None:
        self._guidance_preview = ""
        self.refresh()

    def queue_guidance_echo(self, text: str) -> None:
        clean = _clean(text)
        if not clean.strip():
            return
        self._guidance_echoes.append(clean)
        self.refresh()

    def consume_guidance_echoes(self) -> list[str]:
        echoes = self._guidance_echoes
        self._guidance_echoes = []
        return echoes

    def append_guidance_turn(self, text: str) -> OutputNode | None:
        clean = _clean(text)
        if not clean.strip():
            return None
        preview = strip_pasted_wrapper(clean)
        header, body_lines = self._render_turn_text(preview)
        header = f"[bold white]❯[/] {header}" if header else "[bold white]❯[/]"
        node = self._new_settled_node(
            self._tree.root,
            before_active_stream=True,
            node_type="turn",
            header=header,
            body_lines=body_lines,
            collapsed=False,
        )
        if self._stream_node is None:
            self._current_agent = None
            self._current_tool = None
        self.refresh()
        return node

    def ensure_agent(self) -> OutputNode:
        if self._current_agent is None:
            self._append_root_spacer()
            self._current_agent = self._tree.new_node(
                parent=self._tree.root,
                node_type="assistant",
                header="",
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
        body = Group(*[text_from_line(line) for line in visible]) if visible else Text("")

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
            if node.id not in self._settled_node_ids and not is_transparent_container(node):
                return False
            node = node.parent
        return True

    def safe_flush_line_count(self, width: int, committed: int) -> int:
        lines, line_map = self._tree.render_with_line_map(width)
        return self._safe_flush_limit(lines, line_map, committed)

    def safe_flush_root_slice_line_count(
        self,
        width: int,
        start: int,
        end: int,
        committed: int,
    ) -> int:
        lines, line_map = self._tree.render_root_slice_with_line_map(width, start, end)
        return self._safe_flush_limit(lines, line_map, committed)

    def _safe_flush_limit(
        self,
        lines: list[str],
        line_map: dict[int, str],
        committed: int,
    ) -> int:
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

    def has_active_thinking_stream(self) -> bool:
        node = self._stream_node
        return (
            node is not None
            and node.node_type == "assistant"
            and node.payload.get("phase") == "thinking"
        )

    def active_thinking_stream_node_id(self) -> str | None:
        node = self._stream_node
        if not self.has_active_thinking_stream() or node is None:
            return None
        return node.id

    def active_thinking_stream_line_ids(self, width: int) -> set[int]:
        node = self._stream_node
        if not self.has_active_thinking_stream() or node is None:
            return set()
        _lines, line_map = self._tree.render_with_line_map(width)
        return {index for index, node_id in line_map.items() if node_id == node.id}

    def active_thinking_stream_lines(self, width: int) -> list[str]:
        node = self._stream_node
        if not self.has_active_thinking_stream() or node is None:
            return []
        return self._tree.render_node_lines(node.id, width)

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
