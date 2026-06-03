"""Rich Live/Layout backed bottom input dock."""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from typing import Callable

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.text import Text

from voidx.ui.dock_components.formatting import (
    ANSI_LINE_PREFIX,
    _ansi_line,
    _ansi_rgb,
    _clean,
    _markdown_lines,
    _strip_ansi_trailing_space,
    _text_from_line,
)
from voidx.ui.dock_components.nodes import DockNodeMixin
from voidx.ui.dock_components.state import dock, get_dock, set_dock
from voidx.ui.tree import OutputNode, OutputTree


@dataclass(frozen=True)
class DockStatusRecord:
    status_id: str
    label: str
    detail: str = ""
    stage: str = "working"


def active_agent_step_text() -> str:
    current = get_dock()
    status_record = getattr(current, "status_record", None)
    if not callable(status_record):
        return ""
    record = status_record("agent:-1:progress")
    if record is None:
        return ""
    return _agent_step_text(record.label)


def _agent_step_text(label: str) -> str:
    text = _clean(label).strip()
    prefix = "Agent step "
    if text.startswith(prefix):
        return "step " + text[len(prefix):]
    return text


class BottomInputDock(DockNodeMixin):
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
        self._permission_node: OutputNode | None = None
        self._input_text = ""
        self._cursor_pos = 0
        self._hints: list[tuple[str, str, bool]] = []
        self._refresh_callback: Callable[[], None] | None = None
        self._width_provider: Callable[[], int] | None = None

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

    def set_refresh_callback(self, callback: Callable[[], None] | None) -> None:
        self._refresh_callback = callback

    def set_width_provider(self, callback: Callable[[], int] | None) -> None:
        self._width_provider = callback

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
        self._reset_runtime_nodes()
        self._input_text = ""
        self._cursor_pos = 0
        self._hints = []
        self.refresh()

    def restore_tree(self, tree: OutputTree, *, append: bool = False) -> None:
        if append:
            self._tree.extend_from(tree)
        else:
            self._tree = tree
        self._reset_runtime_nodes()
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
        preview = _clean(text)[:160]
        lines = [_strip_ansi_trailing_space(line) for line in (preview.splitlines() or [preview])]
        self._current_turn = self._tree.new_node(
            parent=self._tree.root,
            node_type="turn",
            header=f"[bold white]❯[/] {escape(lines[0])}",
            body_lines=[escape(line) for line in lines[1:]],
            collapsed=False,
        )
        self.refresh()
        return self._current_turn

    def ensure_agent(self) -> OutputNode:
        if self._current_agent is None:
            self._append_root_spacer()
            self._current_agent = self._tree.new_node(
                parent=self._tree.root,
                node_type="assistant",
                header="[#EBCB8B]●[/#EBCB8B] Working",
                collapsed=False,
            )
            self.refresh()
        return self._current_agent

    def print(self, *args, **kwargs) -> bool:
        if not self._active:
            return False
        self.capture(lambda console: console.print(*args, **kwargs))
        return True

    def capture(self, render: Callable[[Console], None]) -> bool:
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
            self.append_ansi(text)
        return True

    def set_stream(self, text: str, *, parent: OutputNode | None = None) -> bool:
        if not self._active:
            return False
        self._stream_text = text
        self._update_stream_node(parent=parent)
        self.refresh()
        return True

    def commit_stream(self) -> bool:
        if not self._active:
            return False
        self._stream_node = None
        self._stream_text = ""
        self.refresh()
        return True

    def discard_stream(self) -> bool:
        if not self._active:
            return False
        if self._stream_node and not self._stream_text.strip():
            self._remove_node(self._stream_node)
            if self._current_agent is self._stream_node:
                self._current_agent = None
        elif self._stream_node:
            self._remove_node(self._stream_node)
            if self._current_agent is self._stream_node:
                self._current_agent = None
        self._stream_node = None
        self._stream_text = ""
        self.refresh()
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

    def render(self) -> None:
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
        body = Group(*[_text_from_line(line) for line in lines[-body_limit:]]) if lines else Text("")

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

    def _update_stream_node(self, *, parent: OutputNode | None = None) -> None:
        clean = _clean(self._stream_text).strip("\n")
        if not clean:
            return
        if self._stream_node is None or (
            parent is not None and self._stream_node.parent is not parent
        ):
            self._stream_node = self._new_stream_node(parent=parent)
        if clean.startswith("● "):
            clean = clean[2:]
        lines = _markdown_lines(clean, self._markdown_width())
        if not lines:
            return

        # Render the bullet and first line as one ANSI run so the terminal keeps
        # them on the same visual baseline.
        bullet = _ansi_rgb("●", (163, 190, 140))
        self._stream_node.header = _ansi_line(f"{bullet} {lines[0]}")
        self._stream_node.body_lines = [_ansi_line(f"  {line}") for line in lines[1:]]
        self._tree.mark_dirty()

    def _new_stream_node(self, *, parent: OutputNode | None = None) -> OutputNode:
        if parent is not None:
            return self._tree.new_node(
                parent=parent,
                node_type="assistant",
                header="",
                collapsed=False,
            )

        if (
            self._current_agent is not None
            and self._current_agent.node_type == "assistant"
            and self._current_agent.header == "[#EBCB8B]●[/#EBCB8B] Working"
            and not self._current_agent.children
        ):
            return self._current_agent

        if self._current_agent is not None:
            if self._current_agent.header == "[#EBCB8B]●[/#EBCB8B] Working":
                self._current_agent.header = "[dim]●[/dim] voidx"
                self._tree.mark_dirty()
            self._append_root_spacer()
            self._current_agent = self._tree.new_node(
                parent=self._tree.root,
                node_type="assistant",
                header="",
                collapsed=False,
            )
            return self._current_agent

        self._append_root_spacer()
        self._current_agent = self._tree.new_node(
            parent=self._tree.root,
            node_type="assistant",
            header="",
            collapsed=False,
        )
        return self._current_agent

    def record_status(
        self,
        status_id: str,
        label: str,
        detail: str = "",
        *,
        stage: str = "working",
    ) -> DockStatusRecord:
        record = DockStatusRecord(
            status_id=status_id,
            label=label,
            detail=detail,
            stage=stage,
        )
        self._status_records[status_id] = record
        self.refresh()
        return record

    def clear_status_record(self, status_id: str) -> None:
        if status_id in self._status_records:
            self._status_records.pop(status_id, None)
            self.refresh()

    def status_record(self, status_id: str) -> DockStatusRecord | None:
        return self._status_records.get(status_id)

    def _append_root_spacer(self) -> None:
        children = self._tree.root.children
        if not children:
            return
        last = children[-1]
        if last.node_type == "message" and not last.header and not last.body_lines and not last.children:
            return
        self._tree.new_node(
            parent=self._tree.root,
            node_type="message",
            header="",
            collapsed=False,
        )

    def _settle_stream_for_tool(self) -> None:
        if (
            self._stream_node is not None
            and self._stream_node is self._current_agent
            and not self._stream_node.children
            and self._stream_text.strip()
        ):
            self._stream_node.header = "[#EBCB8B]●[/#EBCB8B] Working"
            self._stream_node.body_lines = []
            self._tree.mark_dirty()
        self.commit_stream()

    def _remove_node(self, node: OutputNode) -> None:
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
