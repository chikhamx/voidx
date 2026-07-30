"""Busy activity rendering helpers."""

from __future__ import annotations

import re
import time

from rich.cells import cell_len
from rich.text import Text

from voidx.llm.usage import format_token_count
from voidx.ui.output.dock import (
    active_agent_step_text,
    active_compaction_detail_text,
    active_compaction_text,
    active_error_detail_text,
    active_error_text,
    active_guidance_preview_text,
    active_llm_retry_detail_text,
    active_llm_retry_text,
    active_permission_request_detail_text,
    active_permission_request_text,
    active_turn_analyzing_text,
    dock,
)
from .activity import (
    BUSY_ACTIVITY_GLYPHS,
    BUSY_ACTIVITY_GLYPH_STYLES,
    BUSY_ACTIVITY_STYLE,
)
from .helpers import _clip_cells


LOOP_WAITING_STATUS_ID = "loop:waiting"


BUSY_ACTIVITY_DETAIL_STYLE = "#C9D1D9 on #3a3937"
RETRY_DETAIL_MAX_CELLS = 96
PERMISSION_DETAIL_MAX_LINES = 5
PERMISSION_DETAIL_VERBOSE_KEYS = ("bounds:", "new_string:", "file_path:")
_RETRY_DETAIL_RE = re.compile(r"^retrying in (?P<delay>\d+s):\s*(?P<error>.*)$", re.IGNORECASE)


class _ActivityRendererMixin:
    def _busy_activity_row_count(self, width: int) -> int:
        return len(self._render_busy_activity_elements(width))

    def _render_busy_activity_elements(self, width: int) -> list[Text]:
        if not self._busy and not self._loop_turn_in_progress():
            return self._render_loop_waiting_elements(width)
        elements = [self._busy_activity_text(width)]
        permission_detail = active_permission_request_detail_text()
        if permission_detail:
            for line in _compact_permission_detail_lines(permission_detail):
                elements.append(
                    Text(_full_width_detail_line(line, width), style=BUSY_ACTIVITY_DETAIL_STYLE)
                )
        return elements

    def _render_loop_waiting_elements(self, width: int) -> list[Text]:
        label = self._loop_waiting_label(width)
        if not label:
            return []
        return [Text(label, style=BUSY_ACTIVITY_STYLE)]

    def _loop_waiting_active(self) -> bool:
        status_record = getattr(dock, "status_record", None)
        if not callable(status_record):
            return False
        return status_record(LOOP_WAITING_STATUS_ID) is not None

    def _loop_turn_in_progress(self) -> bool:
        if not getattr(dock, "turn_in_progress", False):
            return False
        metadata = getattr(dock, "current_turn_metadata", None)
        return getattr(metadata, "protocol", "turn") == "loop"

    def _loop_waiting_label(self, width: int) -> str:
        record = dock.status_record(LOOP_WAITING_STATUS_ID) if hasattr(dock, "status_record") else None
        if record is None:
            return ""
        try:
            wake_at = float(record.detail)
        except (TypeError, ValueError):
            return ""
        remaining = int(wake_at - time.time())
        if remaining <= 0:
            return ""
        glyph = BUSY_ACTIVITY_GLYPHS[self._busy_activity_tick % len(BUSY_ACTIVITY_GLYPHS)]
        label = f"{glyph} {record.label} (next round in {self._format_elapsed(remaining)})"
        return _clip_cells(label, width)

    def _busy_activity_text(self, width: int) -> Text:
        label = _clip_cells(self._busy_activity_label(), width)
        if not label:
            return Text()
        text = Text()
        text.append(label[:1], style=self._busy_activity_glyph_style())
        if len(label) > 1:
            text.append(label[1:], style=BUSY_ACTIVITY_STYLE)
        return text

    def _busy_activity_glyph_style(self) -> str:
        return BUSY_ACTIVITY_GLYPH_STYLES[
            self._busy_activity_tick % len(BUSY_ACTIVITY_GLYPH_STYLES)
        ]

    def _busy_activity_label(self) -> str:
        started_at = self._busy_started_at
        glyph = BUSY_ACTIVITY_GLYPHS[
            self._busy_activity_tick % len(BUSY_ACTIVITY_GLYPHS)
        ]
        analyzing = active_turn_analyzing_text()
        compacting = active_compaction_text()
        compaction_detail = active_compaction_detail_text()
        llm_retry = active_llm_retry_text()
        llm_retry_detail = active_llm_retry_detail_text()
        llm_retry_label, llm_retry_error = _format_llm_retry_status(llm_retry, llm_retry_detail)
        error = active_error_text()
        error_detail = active_error_detail_text()
        permission = active_permission_request_text()
        step = active_agent_step_text()
        status_label = permission or error or analyzing or compacting or step or llm_retry_label or ""
        thinking = dock.has_active_thinking_stream()
        current_has_special = bool(thinking or status_label)
        if self._busy_activity_prev_has_special and not current_has_special:
            self._busy_activity_verb = self._choose_busy_activity_verb()
        self._busy_activity_prev_has_special = current_has_special
        default_turn_verb = "Thinking" if self._loop_turn_in_progress() else ""
        verb = "Thinking" if thinking else status_label or self._busy_activity_verb or default_turn_verb
        prefix = f"{glyph} {verb}"
        if started_at is None:
            return prefix
        elapsed = max(0, int(time.monotonic() - started_at))
        details = [self._format_elapsed(elapsed)]
        if permission and status_label != permission:
            details.append(permission)
        if analyzing and status_label != analyzing:
            details.append(analyzing)
        if compacting and status_label != compacting:
            details.append(compacting)
        if step and status_label != step:
            details.append(step)
        token_text = self._turn_token_text()
        if token_text:
            details.append(token_text)
        latest = self._latest_action_text()
        if latest:
            details.append(latest)
        if compacting and compaction_detail:
            details.append(compaction_detail)
        if error and error_detail:
            details.append(error_detail)
        if llm_retry_label and status_label == llm_retry_label and llm_retry_error:
            details.append(llm_retry_error)
        preview = active_guidance_preview_text()
        if preview:
            details.append(f"⚡{_clip_cells(preview, 40)}")
        return f"{prefix} ({' '.join(details)})"

    def _turn_token_text(self) -> str:
        stats = getattr(self.status, "usage_stats", None)
        if stats is None:
            return ""
        turn_in = getattr(stats, "turn_input_tokens", 0)
        turn_out = getattr(stats, "turn_output_tokens", 0)
        if turn_in <= 0 and turn_out <= 0:
            return ""
        return f"↑{format_token_count(turn_in)} ↓{format_token_count(turn_out)}"

    def _latest_action_text(self) -> str:
        fn = getattr(self.status, "latest_action", None)
        if fn is None:
            return ""
        action = fn()
        if not action:
            return ""
        return f"→{action}"

    @staticmethod
    def _format_elapsed(seconds: int) -> str:
        if seconds < 60:
            return f"{seconds}s"
        minutes, remainder = divmod(seconds, 60)
        return f"{minutes}m {remainder}s"


def _full_width_detail_line(line: str, width: int) -> str:
    clipped = _clip_cells(line, width)
    padding = max(0, width - cell_len(clipped))
    return clipped + (" " * padding)


def _compact_permission_detail_lines(detail: str) -> list[str]:
    lines = [line for line in detail.splitlines() if line.strip()]
    if len(lines) <= PERMISSION_DETAIL_MAX_LINES:
        return lines

    compacted = _permission_tool_summary_lines(lines)
    if not compacted:
        compacted = [
            line for line in lines
            if not _is_verbose_permission_detail_line(line)
        ]
    visible = compacted[:PERMISSION_DETAIL_MAX_LINES]
    remaining = len(compacted) - len(visible)
    if remaining > 0:
        visible[-1] = f"   ... {remaining} more tool{'s' if remaining != 1 else ''}"
    return visible


def _permission_tool_summary_lines(lines: list[str]) -> list[str]:
    result: list[str] = []
    current_name = ""
    current_target = ""
    for line in lines:
        if _is_permission_tool_header(line):
            if current_name:
                result.append(_permission_tool_summary_line(current_name, current_target))
            current_name = line.strip()
            current_target = ""
            continue
        stripped = line.strip()
        if current_name and stripped.startswith("target:") and not current_target:
            current_target = stripped.split(":", 1)[1].strip()
    if current_name:
        result.append(_permission_tool_summary_line(current_name, current_target))
    return result


def _permission_tool_summary_line(name: str, target: str) -> str:
    return f"{name} -> {target}" if target else name


def _is_permission_tool_header(line: str) -> bool:
    stripped = line.strip()
    head, separator, tail = stripped.partition(". ")
    return bool(separator and head.isdigit() and tail)


def _is_verbose_permission_detail_line(line: str) -> bool:
    stripped = line.strip()
    return any(stripped.startswith(key) for key in PERMISSION_DETAIL_VERBOSE_KEYS)


def _format_llm_retry_status(label: str, detail: str) -> tuple[str, str]:
    if not label:
        return "", ""
    clean_detail = detail.strip()
    match = _RETRY_DETAIL_RE.match(clean_detail)
    if not match:
        return label, _clip_retry_error(clean_detail)
    delay = match.group("delay")
    error = match.group("error").strip()
    return f"{label} in {delay}", _clip_retry_error(error)


def _clip_retry_error(error: str) -> str:
    if not error:
        return ""
    return _clip_cells(error, RETRY_DETAIL_MAX_CELLS)
