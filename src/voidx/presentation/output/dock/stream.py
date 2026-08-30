"""Streaming output node management for BottomInputDock."""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.markup import escape
from rich.text import Text

from voidx.presentation.output.dock.agent_placeholder import is_agent_placeholder_header
from voidx.presentation.output.dock.formatting import (
    ANSI_LINE_PREFIX,
    _ansi_line,
    _ansi_rgb,
    _clean,
    _markdown_lines,
)
from voidx.presentation.output.dock.stream_projection import (
    StreamProjectionUpdate,
    StreamingMarkdownProjection,
)
from voidx.presentation.output.tree import OutputNode


@dataclass(frozen=True)
class StreamCommitWorkItem:
    node_id: str
    parent_id: str | None
    revision: int
    generation: int
    raw_text: str
    phase: str
    width: int


@dataclass(frozen=True)
class StreamCommitProjection:
    header: str
    body_lines: tuple[str, ...]
    raw_text: str
    phase: str


def _stream_projection(
    raw_text: str,
    phase: str,
    width: int,
    *,
    plain: bool = False,
) -> StreamCommitProjection:
    clean = _clean(raw_text).strip("\n")
    if phase == "thinking":
        lines = _thinking_visual_lines(clean, width)
        visible = lines[-5:]
        return StreamCommitProjection(
            header="",
            body_lines=tuple(_ansi_line(f"  {escape(line)}") for line in visible),
            raw_text=clean,
            phase="thinking",
        )

    content = clean[2:] if clean.startswith("● ") else clean
    if plain:
        lines = content.splitlines() or [content]
        if not any(line.strip() for line in lines):
            return StreamCommitProjection("", (), content, "text")
        return StreamCommitProjection(
            header=escape(f"● {lines[0]}"),
            body_lines=tuple(escape(f"  {line}") for line in lines[1:]),
            raw_text=content,
            phase="text",
        )

    lines = _markdown_lines(content, width)
    if not lines:
        return StreamCommitProjection("", (), content, "text")
    bullet = _ansi_rgb("●", (163, 190, 140))
    return StreamCommitProjection(
        header=_ansi_line(f"{bullet} {lines[0]}"),
        body_lines=tuple(_ansi_line(f"  {line}") for line in lines[1:]),
        raw_text=content,
        phase="text",
    )


def build_canonical_stream_projection(
    work_item: StreamCommitWorkItem,
) -> StreamCommitProjection:
    """Build the canonical Markdown projection from immutable stream data."""
    return _stream_projection(work_item.raw_text, work_item.phase, work_item.width)


def build_plain_stream_projection(
    work_item: StreamCommitWorkItem,
) -> StreamCommitProjection:
    """Build an escaped projection that never interprets stream text as markup."""
    return _stream_projection(
        work_item.raw_text,
        work_item.phase,
        work_item.width,
        plain=True,
    )


def _clean_stream_text(text: str) -> str:
    source = str(text or "")
    trailing_newlines = len(source) - len(source.rstrip("\n"))
    return _clean(source) + "\n" * trailing_newlines


def _strip_assistant_bullet(text: str) -> str:
    return text[2:] if text.startswith("● ") else text


def _prefixed_projection_line(prefix: str, line: str) -> str:
    if line.startswith(ANSI_LINE_PREFIX):
        return _ansi_line(prefix + line[len(ANSI_LINE_PREFIX):])
    return prefix + line


def _install_projection_update(
    node: OutputNode,
    update: StreamProjectionUpdate,
) -> tuple[int, int, list[str], str | None, list[str]] | None:
    replace_from = update.replace_from
    lines = list(update.replacement_lines)
    if replace_from <= 0:
        if not lines:
            node.header = ""
            node.body_lines = []
            return None
        bullet = _ansi_rgb("●", (163, 190, 140))
        node.header = _prefixed_projection_line(f"{bullet} ", lines[0])
        node.body_lines = [
            _prefixed_projection_line("  ", line) for line in lines[1:]
        ]
        return None
    body_start = max(replace_from - 1, 0)
    old_body_line_count = len(node.body_lines)
    old_suffix = list(node.body_lines[body_start:])
    old_anchor = node.body_lines[body_start - 1] if body_start > 0 else None
    replacement_body_lines = [
        _prefixed_projection_line("  ", line) for line in lines
    ]
    node.body_lines[body_start:] = replacement_body_lines
    return (
        body_start,
        old_body_line_count,
        old_suffix,
        old_anchor,
        replacement_body_lines,
    )


class DockStreamMixin:
    def set_stream(
        self,
        text: str,
        *,
        parent: OutputNode | None = None,
        phase: str = "text",
        refresh: bool = True,
        snapshot_contract: str = "cumulative",
    ) -> bool:
        if not self._active:
            return False
        if snapshot_contract not in {"cumulative", "delta"}:
            raise ValueError("stream snapshot contract must be cumulative or delta")
        self._stream_revision += 1
        self._update_stream_node(
            text,
            parent=parent,
            phase=phase,
            snapshot_contract=snapshot_contract,
        )
        if self._stream_node is not None:
            self._stream_node.payload["stream_revision"] = self._stream_revision
        self._mark_unsettled(self._stream_node)
        if refresh:
            self.refresh()
        return True

    def commit_stream(self, *, refresh: bool = True) -> bool:
        if not self._active:
            return False
        work_item = self.prepare_stream_commit(refresh=False)
        if work_item is None:
            if refresh:
                self.refresh()
            return True
        try:
            projection = build_canonical_stream_projection(work_item)
        except Exception:
            projection = build_plain_stream_projection(work_item)
        return self.apply_stream_commit(work_item, projection, refresh=refresh)

    def prepare_stream_commit(
        self,
        *,
        refresh: bool = True,
    ) -> StreamCommitWorkItem | None:
        if not self._active:
            return None
        if self._ignored_duplicate_stream_commit:
            self._ignored_duplicate_stream_commit = False
            self._clear_stream_projection_state()
            if refresh:
                self.refresh()
            return None

        stream_node = self._stream_node
        if stream_node is None or stream_node is self._current_agent:
            self._stream_node = None
            self._clear_stream_projection_state()
            if refresh:
                self.refresh()
            return None

        phase = str(stream_node.payload.get("phase") or "text")
        if phase == "thinking":
            self._remove_node(stream_node)
            self._stream_node = None
            self._clear_stream_projection_state()
            if refresh:
                self.refresh()
            return None

        raw_text = _clean(self._stream_text).strip("\n")
        work_item = StreamCommitWorkItem(
            node_id=stream_node.id,
            parent_id=stream_node.parent.id if stream_node.parent else None,
            revision=self._stream_revision,
            generation=self._stream_generation,
            raw_text=raw_text,
            phase=phase,
            width=self._markdown_width(),
        )
        stream_node.payload["render_pending"] = True
        stream_node.payload["stream_revision"] = work_item.revision
        self._pending_stream_commits[stream_node.id] = work_item
        self._mark_unsettled(stream_node)
        self._stream_node = None
        self._clear_stream_projection_state()
        if refresh:
            self.refresh()
        return work_item

    def apply_stream_commit(
        self,
        work_item: StreamCommitWorkItem,
        projection: StreamCommitProjection,
        *,
        refresh: bool = True,
    ) -> bool:
        if self._pending_stream_commits.get(work_item.node_id) != work_item:
            return False
        if work_item.generation != self._stream_generation:
            self._pending_stream_commits.pop(work_item.node_id, None)
            return False
        stream_node = self._tree.get(work_item.node_id)
        if stream_node is None:
            self._pending_stream_commits.pop(work_item.node_id, None)
            return False
        if stream_node.payload.get("stream_revision") != work_item.revision:
            self._pending_stream_commits.pop(work_item.node_id, None)
            return False

        stream_node.header = projection.header
        stream_node.body_lines = list(projection.body_lines)
        stream_node.payload["raw_text"] = projection.raw_text
        stream_node.payload.pop("phase", None)
        stream_node.payload.pop("render_pending", None)
        stream_node.payload.pop("stream_revision", None)
        self._mark_settled(stream_node)
        self._last_committed_stream_text = _stream_signature(work_item.raw_text)
        self._last_committed_stream_parent_id = work_item.parent_id
        self._last_committed_stream_node_id = work_item.node_id
        self._pending_stream_commits.pop(work_item.node_id, None)
        self._tree.mark_dirty(stream_node.id)
        if refresh:
            self.refresh()
        return True

    def discard_stream(self, *, refresh: bool = True) -> bool:
        if not self._active:
            return False
        if self._stream_node:
            self._remove_node(self._stream_node)
            if self._current_agent is self._stream_node:
                self._current_agent = None
        elif self._pending_stream_commits:
            node_id = next(reversed(self._pending_stream_commits))
            self._pending_stream_commits.pop(node_id, None)
            node = self._tree.get(node_id)
            if node is not None:
                self._remove_node(node)
        self._stream_node = None
        self._clear_stream_projection_state()
        self._last_committed_stream_text = ""
        self._last_committed_stream_parent_id = None
        self._last_committed_stream_node_id = None
        self._ignored_duplicate_stream_commit = False
        if refresh:
            self.refresh()
        return True

    def _clear_stream_projection_state(self) -> None:
        self._stream_text = ""
        self._stream_thinking_text = ""
        self._stream_projection = None
        self._stream_projection_phase = None


    def _update_stream_node(
        self,
        text: str,
        *,
        parent: OutputNode | None = None,
        phase: str = "text",
        snapshot_contract: str = "cumulative",
    ) -> None:
        incoming = _clean_stream_text(text)
        stream_existed = self._stream_node is not None
        parent_changed = (
            self._stream_node is not None
            and parent is not None
            and self._stream_node.parent is not parent
        )
        phase_changed = self._stream_projection_phase not in {None, phase}
        if parent_changed or phase_changed:
            self._stream_projection = None
            self._stream_projection_phase = None
            if phase == "thinking":
                self._stream_thinking_text = ""
            else:
                self._stream_text = ""

        if phase == "thinking":
            self._stream_thinking_text = (
                self._stream_thinking_text + incoming
                if snapshot_contract == "delta"
                else incoming
            )
            clean = self._stream_thinking_text.strip("\n")
            if not clean:
                return
            if self._stream_node is None or parent_changed:
                self._stream_node = self._new_stream_node(parent=parent)
            lines = _thinking_visual_lines(clean, self._markdown_width())
            visible = lines[-5:]
            self._stream_node.header = ""
            self._stream_node.body_lines = [
                _ansi_line(f"  {escape(line)}") for line in visible
            ]
            self._stream_node.payload["phase"] = "thinking"
            self._stream_node.payload["raw_text"] = clean
            self._stream_projection = None
            self._stream_projection_phase = "thinking"
            if stream_existed and not parent_changed:
                self._tree.mark_dirty(self._stream_node.id)
            else:
                self._tree.mark_dirty()
            return

        previous = self._stream_text
        if snapshot_contract == "cumulative" or not previous:
            incoming = _strip_assistant_bullet(incoming)
        if snapshot_contract == "delta":
            canonical = previous + incoming
            operation = "append" if previous else "replace"
            projection_input = incoming if previous else canonical
        else:
            canonical = incoming
            if previous and canonical.startswith(previous) and not phase_changed:
                operation = "append"
                projection_input = canonical[len(previous):]
            else:
                operation = "replace"
                projection_input = canonical
        clean = canonical.strip("\n")
        if not clean:
            return
        target = parent or self.ensure_agent()
        if self._is_duplicate_committed_stream(clean, target):
            self._ignored_duplicate_stream_commit = True
            self._stream_text = canonical
            return
        if self._stream_node is None or parent_changed:
            self._stream_node = self._new_stream_node(parent=parent)
            operation = "replace"
            projection_input = canonical
        self._stream_text = canonical

        width = self._markdown_width()
        projection = self._stream_projection
        if not isinstance(projection, StreamingMarkdownProjection):
            projection = StreamingMarkdownProjection(width=width)
            self._stream_projection = projection
            operation = "replace"
            projection_input = canonical
        elif projection.width != width:
            projection = StreamingMarkdownProjection(width=width)
            self._stream_projection = projection
            operation = "replace"
            projection_input = canonical
        update = projection.update(projection_input, operation=operation)
        suffix_update = _install_projection_update(self._stream_node, update)
        cache_spliced = False
        if (
            suffix_update is not None
            and stream_existed
            and not parent_changed
            and not phase_changed
        ):
            cache_spliced = self._tree.splice_cached_node_body_suffix(
                self._stream_node.id,
                *suffix_update,
            )
        self._stream_projection_phase = "text"
        self._stream_node.payload.pop("phase", None)
        self._stream_node.payload["raw_text"] = clean
        if self._stream_thinking_text.strip():
            self._stream_node.payload["thinking_text"] = self._stream_thinking_text.strip(
                "\n"
            )
        if stream_existed and not parent_changed and not phase_changed:
            if not cache_spliced:
                self._tree.mark_dirty(self._stream_node.id)
        else:
            self._tree.mark_dirty()

    def _new_stream_node(self, *, parent: OutputNode | None = None) -> OutputNode:
        target = parent or self.ensure_agent()
        if parent is None and is_agent_placeholder_header(target.header):
            target.header = ""
            target.body_lines = []
            self._tree.mark_dirty()
        return self._tree.new_node(
            parent=target,
            node_type="assistant",
            header="",
            collapsed=False,
        )

    def _settle_stream_for_tool(self) -> None:
        self.commit_stream()

    def _is_duplicate_committed_stream(self, text: str, target: OutputNode) -> bool:
        signature = _stream_signature(text)
        if not signature:
            return False
        if any(
            work_item.parent_id == target.id
            and _stream_signature(work_item.raw_text) == signature
            for work_item in self._pending_stream_commits.values()
        ):
            return True
        if signature != self._last_committed_stream_text:
            return False
        if target.id != self._last_committed_stream_parent_id:
            return False
        node_id = self._last_committed_stream_node_id
        if not node_id:
            return False
        last = self._tree.get(node_id)
        return (
            last is not None
            and last.node_type == "assistant"
            and last.id in self._settled_node_ids
        )


def _thinking_visual_lines(text: str, width: int) -> list[str]:
    console = Console(width=max(width, 1), force_terminal=True, _environ={})
    lines: list[str] = []
    for logical_line in text.splitlines() or [text]:
        wrapped = Text(logical_line).wrap(console, max(width, 1))
        if wrapped:
            lines.extend(wrapped_line.plain for wrapped_line in wrapped)
        else:
            lines.append("")
    return lines


def _stream_signature(text: str) -> str:
    clean = _clean(text).strip()
    if clean.startswith("● "):
        clean = clean[2:].lstrip()
    return clean
