"""Bounded provisional Markdown projection for active assistant streams."""

from __future__ import annotations

import re
from dataclasses import dataclass

from markdown_it import MarkdownIt
from rich.cells import cell_len, chop_cells
from rich.markup import escape

from voidx.presentation.output.dock.formatting import _ansi_line, _markdown_lines

STREAMING_TAIL_SOFT_LIMIT = 8 * 1024
STREAMING_TAIL_HARD_LIMIT = 16 * 1024

_MARKDOWN = MarkdownIt()
_HTML_RE = re.compile(r"</?[A-Za-z!][^>]*>", re.DOTALL)
_REFERENCE_DEFINITION_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:", re.MULTILINE)
_REFERENCE_LINK_RE = re.compile(r"\[[^\]]+\]\s*\[[^\]]*\]")
_LIST_OR_QUOTE_RE = re.compile(r"^\s{0,3}(?:[-+*]|\d+[.)]|>)\s", re.MULTILINE)
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})", re.MULTILINE)


@dataclass(frozen=True)
class StreamProjectionUpdate:
    """Suffix replacement required to install one projection update."""

    replace_from: int
    replacement_lines: tuple[str, ...]


class StreamingMarkdownProjection:
    """Keep stream preview parsing bounded while retaining canonical raw text."""

    def __init__(self, *, width: int) -> None:
        self.width = max(width, 1)
        self.raw_text = ""
        self._mutable_tail = ""
        self._frozen_lines: list[str] = []
        self._preview_lines: list[str] = []
        self._provisional_chunk_count = 0
        self._stable_block_count = 0

    @property
    def preview_lines(self) -> list[str]:
        return self._preview_lines

    @property
    def mutable_tail_length(self) -> int:
        return len(self._mutable_tail)

    @property
    def provisional_chunk_count(self) -> int:
        return self._provisional_chunk_count

    @property
    def stable_block_count(self) -> int:
        return self._stable_block_count

    def update(
        self,
        text: str,
        *,
        operation: str = "append",
    ) -> StreamProjectionUpdate:
        incoming = str(text or "")
        if operation not in {"append", "replace"}:
            raise ValueError("stream projection operation must be append or replace")
        if operation == "replace":
            self._clear()
            replace_from = 0
            self.raw_text = incoming
        else:
            replace_from = len(self._frozen_lines)
            self.raw_text += incoming

        self._feed(incoming)
        mutable_lines = self._render_mutable_tail()
        replacement = self._frozen_lines[replace_from:] + mutable_lines
        self._preview_lines[replace_from:] = replacement
        return StreamProjectionUpdate(replace_from, tuple(replacement))

    def resize(self, width: int) -> StreamProjectionUpdate:
        next_width = max(width, 1)
        if next_width == self.width:
            return StreamProjectionUpdate(len(self._preview_lines), ())
        raw_text = self.raw_text
        self.width = next_width
        self._clear()
        self.raw_text = raw_text
        self._feed(raw_text)
        mutable_lines = self._render_mutable_tail()
        replacement = self._frozen_lines + mutable_lines
        self._preview_lines[:] = replacement
        return StreamProjectionUpdate(0, tuple(replacement))

    def _clear(self) -> None:
        self.raw_text = ""
        self._mutable_tail = ""
        self._frozen_lines.clear()
        self._preview_lines.clear()
        self._provisional_chunk_count = 0
        self._stable_block_count = 0

    def _feed(self, text: str) -> None:
        remaining = text
        while remaining:
            if len(self._mutable_tail) >= STREAMING_TAIL_HARD_LIMIT:
                self._freeze_provisional_prefix()
            capacity = STREAMING_TAIL_HARD_LIMIT - len(self._mutable_tail)
            take = min(len(remaining), max(capacity, 1))
            self._mutable_tail += remaining[:take]
            remaining = remaining[take:]
            self._freeze_stable_prefix()
            if remaining and len(self._mutable_tail) >= STREAMING_TAIL_HARD_LIMIT:
                self._freeze_provisional_prefix()

    def _freeze_stable_prefix(self) -> None:
        if self._provisional_chunk_count > 0:
            return
        stable_length = stable_markdown_prefix_length(self._mutable_tail)
        if stable_length <= 0:
            return
        stable_text = self._mutable_tail[:stable_length]
        self._mutable_tail = self._mutable_tail[stable_length:]
        lines = render_markdown_lines(stable_text, self.width)
        self._append_frozen_lines(lines)
        self._stable_block_count += 1

    def _freeze_provisional_prefix(self) -> None:
        if len(self._mutable_tail) <= STREAMING_TAIL_SOFT_LIMIT:
            return
        requested = len(self._mutable_tail) - STREAMING_TAIL_SOFT_LIMIT
        boundary = _safe_provisional_boundary(
            self._mutable_tail,
            requested,
            self.width,
        )
        if boundary <= 0:
            boundary = requested
        provisional = self._mutable_tail[:boundary]
        self._mutable_tail = self._mutable_tail[boundary:]
        self._append_frozen_lines(_escaped_plain_lines(provisional, self.width))
        self._provisional_chunk_count += 1

    def _append_frozen_lines(self, lines: list[str]) -> None:
        if not lines:
            return
        self._frozen_lines.extend(lines)

    def _render_mutable_tail(self) -> list[str]:
        if not self._mutable_tail:
            return []
        if self._provisional_chunk_count > 0 or _parser_uncertain(self._mutable_tail):
            return _escaped_plain_lines(self._mutable_tail, self.width)
        return render_markdown_lines(self._mutable_tail, self.width)


def render_markdown_lines(text: str, width: int) -> list[str]:
    """Render one bounded Markdown fragment."""
    if not text:
        return []
    return [_ansi_line(line) for line in _markdown_lines(text, width)]


def stable_markdown_prefix_length(text: str) -> int:
    """Return a parser-proven stable prefix without inspecting future input."""
    if not text or _contains_cross_block_syntax(text):
        return 0
    lines = text.splitlines(keepends=True)
    if not lines:
        return 0
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    try:
        tokens = _MARKDOWN.parse(text)
    except Exception:
        return 0

    stable_end = 0
    for token in tokens:
        if token.level != 0 or token.map is None:
            continue
        start_line, end_line = token.map
        if start_line < 0 or end_line > len(lines) or start_line > end_line:
            break
        if starts[start_line] < stable_end:
            continue
        raw = text[starts[start_line]:starts[end_line]]
        token_type = token.type
        if token_type == "paragraph_open":
            if not _has_blank_line_after(lines, end_line):
                break
        elif token_type == "heading_open":
            if not raw.endswith(("\n", "\r")):
                break
        elif token_type == "hr":
            if not raw.endswith(("\n", "\r")):
                break
        elif token_type == "fence":
            if not _has_closed_fence(raw):
                break
        else:
            break

        consume_line = end_line
        while consume_line < len(lines) and not lines[consume_line].strip():
            consume_line += 1
        stable_end = starts[consume_line]
    return stable_end


def _render_mutable_tail(text: str, width: int) -> list[str]:
    if not text:
        return []
    if _parser_uncertain(text):
        return _escaped_plain_lines(text, width)
    return render_markdown_lines(text, width)


def _parser_uncertain(text: str) -> bool:
    return (
        _has_unclosed_fence(text)
        or bool(_HTML_RE.search(text))
        or bool(_REFERENCE_DEFINITION_RE.search(text))
        or bool(_REFERENCE_LINK_RE.search(text))
        or bool(_LIST_OR_QUOTE_RE.search(text))
    )


def _contains_cross_block_syntax(text: str) -> bool:
    return bool(
        _HTML_RE.search(text)
        or _REFERENCE_DEFINITION_RE.search(text)
        or _REFERENCE_LINK_RE.search(text)
        or _LIST_OR_QUOTE_RE.search(text)
    )


def _has_blank_line_after(lines: list[str], end_line: int) -> bool:
    return end_line < len(lines) and not lines[end_line].strip()


def _has_closed_fence(text: str) -> bool:
    opening = re.match(r"^\s{0,3}(`{3,}|~{3,})[^\n]*(?:\r?\n|$)", text)
    if opening is None:
        return False
    marker = opening.group(1)[0]
    size = len(opening.group(1))
    closing = re.compile(rf"^\s{{0,3}}{re.escape(marker)}{{{size},}}\s*$", re.MULTILINE)
    return closing.search(text[opening.end():]) is not None


def _has_unclosed_fence(text: str) -> bool:
    fences = _FENCE_RE.findall(text)
    return len(fences) % 2 == 1


def _safe_provisional_boundary(text: str, requested: int, width: int) -> int:
    bounded = max(0, min(requested, len(text)))
    line_boundary = text.rfind("\n", 0, bounded + 1)
    if line_boundary >= 0:
        return line_boundary + 1

    chunks = chop_cells(text[:bounded], max(width, 1))
    if chunks and cell_len(chunks[-1]) < max(width, 1):
        chunks.pop()
    return sum(len(chunk) for chunk in chunks)


def _escaped_plain_lines(text: str, width: int) -> list[str]:
    if not text:
        return []
    lines: list[str] = []
    for logical_line in text.splitlines() or [text]:
        chunks = chop_cells(logical_line, max(width, 1)) or [""]
        lines.extend(_ansi_line(chunk) for chunk in chunks)
    return lines
