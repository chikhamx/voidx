"""Grouped runtime state for the pure terminal TUI."""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from rich.console import Console


@dataclass
class InputState:
    lines: list[str] = field(default_factory=lambda: [""])
    cursor_row: int = 0
    cursor_col: int = 0
    history: list[str] = field(default_factory=list)
    history_paste_entries: list[list[dict[str, Any]]] = field(default_factory=list)
    history_idx: int = -1
    history_draft: list[str] = field(default_factory=lambda: [""])
    history_draft_paste_entries: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SubmitState:
    queue: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    busy: bool = False
    current_task: asyncio.Task[bool] | None = None
    current_text: str = ""
    current_paste_entries: list[dict[str, Any]] = field(default_factory=list)
    cancel_requested: bool = False
    ctrl_c_armed: bool = False
    ctrl_c_deadline: float = 0.0
    quiet_commands: list[str] = field(default_factory=list)


@dataclass
class ChoiceState:
    queue: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    prompt_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active: list[tuple[str, str, str]] | None = None
    prompt: str = ""
    selected: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)
    anchor: str = ""


@dataclass
class TextPromptState:
    queue: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    active: str | None = None
    default: str = ""
    secret: bool = False
    saved_input_lines: list[str] = field(default_factory=lambda: [""])
    saved_cursor_row: int = 0
    saved_cursor_col: int = 0


@dataclass
class PanelState:
    command_selected: int = 0
    command_active: bool = False
    attachment_selected: int = 0
    attachment_suppressed_text: str = ""
    attachment_matches_cache_key: tuple[str, str, int, int] | None = None
    attachment_matches_cache: list[Any] = field(default_factory=list)
    skill_selected: int = 0
    skill_suppressed_text: str = ""
    skill_matches_cache_key: tuple[str, str, int, int] | None = None
    skill_matches_cache: list[Any] = field(default_factory=list)
    skill_service_cache_key: tuple[Any, ...] | None = None
    skill_service_cache: Any | None = None


@dataclass
class CaptureState:
    buffer: io.StringIO | None = None
    console: Console | None = None
    console_key: tuple[int, int | None] | None = None


@dataclass(frozen=True)
class StatusSummaryCache:
    width: int
    snapshot: tuple[Any, ...]
    summary: str
    segments: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RenderStats:
    total_lines: int
    changed_lines: int
    render_ms: float
    strategy: str


@dataclass
class RenderState:
    running: bool = False
    exit_requested: bool = False
    last_error: str = ""
    notice: str = ""
    pending_tb: str = ""
    has_rendered_frame: bool = False
    cursor_to_frame_top_lines: int = 0
    cursor_to_frame_end_lines: int = 0
    last_frame_rows: int = 0
    last_frame_start_row: int = 1
    last_bottom_rows: int = 0
    last_bottom_start_row: int = 1
    input_region_render_pending: bool = False
    choice_selection_render_pending: bool = False
    committed_line_count: int = 0
    visible_committed_rows: int = 0
    was_busy: bool = False
    render_scheduled: bool = False
    status_summary_dirty: bool = True
    status_summary_cache: StatusSummaryCache | None = None
    busy_started_at: float | None = None
    busy_activity_verb: str = ""
    busy_activity_tick: int = 0
    busy_activity_prev_has_special: bool = False
    busy_activity_timer_task: asyncio.Task[None] | None = None
    last_busy_activity_rows: int = 0
    last_busy_activity_start_row: int = 0
    panel_row_limit: int | None = None
    base_bottom_rows_cache_key: tuple[Any, ...] | None = None
    base_bottom_rows_cache_count: int = 0
    prev_frame_lines: list[str] | None = None
    prev_frame_start_row: int = 1
    prev_frame_width: int = 0
    prev_frame_term_height: int | None = None
    render_stats: RenderStats | None = None


@dataclass
class ExternalState:
    request_handler: Callable[[Any], Awaitable[Any]] | None = None
    command_handler: Callable[[Any], Awaitable[Any]] | None = None
    mcp_catalog_provider: Callable[[], list] | None = None


@dataclass
class TerminalState:
    stdin_fd: int | None = None
    tty: bool = False
    old_termios: list | None = None
    windows_stdout_mode: int | None = None
    stdin_reader: asyncio.StreamReader | None = None
    stdin_transport: asyncio.Transport | None = None
    stdin_pipe: Any = None


@dataclass
class PasteState:
    pending_bytes: bytes = b""
    paste_buffer: bytes | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)
    next_id: int = 1
    clipboard_change_count: int = -1


STATE_FIELD_MAP: dict[str, tuple[str, str]] = {
    "_input_lines": ("_input_state", "lines"),
    "_cursor_row": ("_input_state", "cursor_row"),
    "_cursor_col": ("_input_state", "cursor_col"),
    "_input_history": ("_input_state", "history"),
    "_input_history_paste_entries": ("_input_state", "history_paste_entries"),
    "_history_idx": ("_input_state", "history_idx"),
    "_history_draft": ("_input_state", "history_draft"),
    "_history_draft_paste_entries": ("_input_state", "history_draft_paste_entries"),
    "_queue": ("_submit_state", "queue"),
    "_busy": ("_submit_state", "busy"),
    "_current_submit_task": ("_submit_state", "current_task"),
    "_current_submitted_text": ("_submit_state", "current_text"),
    "_current_submitted_paste_entries": ("_submit_state", "current_paste_entries"),
    "_submit_cancel_requested": ("_submit_state", "cancel_requested"),
    "_ctrl_c_armed": ("_submit_state", "ctrl_c_armed"),
    "_ctrl_c_deadline": ("_submit_state", "ctrl_c_deadline"),
    "_quiet_commands": ("_submit_state", "quiet_commands"),
    "_choice_queue": ("_choice_state", "queue"),
    "_active_choice": ("_choice_state", "active"),
    "_choice_prompt": ("_choice_state", "prompt"),
    "_choice_selected": ("_choice_state", "selected"),
    "_choice_details": ("_choice_state", "details"),
    "_choice_anchor": ("_choice_state", "anchor"),
    "_text_queue": ("_text_prompt_state", "queue"),
    "_active_text_prompt": ("_text_prompt_state", "active"),
    "_active_text_default": ("_text_prompt_state", "default"),
    "_active_text_secret": ("_text_prompt_state", "secret"),
    "_saved_input_lines": ("_text_prompt_state", "saved_input_lines"),
    "_saved_cursor_row": ("_text_prompt_state", "saved_cursor_row"),
    "_saved_cursor_col": ("_text_prompt_state", "saved_cursor_col"),
    "_command_selected": ("_panel_state", "command_selected"),
    "_command_panel_active": ("_panel_state", "command_active"),
    "_attachment_selected": ("_panel_state", "attachment_selected"),
    "_attachment_panel_suppressed_text": ("_panel_state", "attachment_suppressed_text"),
    "_attachment_matches_cache_key": ("_panel_state", "attachment_matches_cache_key"),
    "_attachment_matches_cache": ("_panel_state", "attachment_matches_cache"),
    "_skill_selected": ("_panel_state", "skill_selected"),
    "_skill_panel_suppressed_text": ("_panel_state", "skill_suppressed_text"),
    "_skill_matches_cache_key": ("_panel_state", "skill_matches_cache_key"),
    "_skill_matches_cache": ("_panel_state", "skill_matches_cache"),
    "_skill_service_cache_key": ("_panel_state", "skill_service_cache_key"),
    "_skill_service_cache": ("_panel_state", "skill_service_cache"),
    "_capture_buffer": ("_capture_state", "buffer"),
    "_capture_console": ("_capture_state", "console"),
    "_capture_console_key": ("_capture_state", "console_key"),
    "_running": ("_render_state", "running"),
    "_exit_requested": ("_render_state", "exit_requested"),
    "_last_error": ("_render_state", "last_error"),
    "_notice": ("_render_state", "notice"),
    "_pending_tb": ("_render_state", "pending_tb"),
    "_has_rendered_frame": ("_render_state", "has_rendered_frame"),
    "_cursor_to_frame_top_lines": ("_render_state", "cursor_to_frame_top_lines"),
    "_cursor_to_frame_end_lines": ("_render_state", "cursor_to_frame_end_lines"),
    "_last_frame_rows": ("_render_state", "last_frame_rows"),
    "_last_frame_start_row": ("_render_state", "last_frame_start_row"),
    "_last_bottom_rows": ("_render_state", "last_bottom_rows"),
    "_last_bottom_start_row": ("_render_state", "last_bottom_start_row"),
    "_input_region_render_pending": ("_render_state", "input_region_render_pending"),
    "_choice_selection_render_pending": ("_render_state", "choice_selection_render_pending"),
    "_committed_line_count": ("_render_state", "committed_line_count"),
    "_visible_committed_rows": ("_render_state", "visible_committed_rows"),
    "_was_busy": ("_render_state", "was_busy"),
    "_render_scheduled": ("_render_state", "render_scheduled"),
    "_busy_started_at": ("_render_state", "busy_started_at"),
    "_busy_activity_verb": ("_render_state", "busy_activity_verb"),
    "_busy_activity_tick": ("_render_state", "busy_activity_tick"),
    "_busy_activity_prev_has_special": ("_render_state", "busy_activity_prev_has_special"),
    "_busy_activity_timer_task": ("_render_state", "busy_activity_timer_task"),
    "_last_busy_activity_rows": ("_render_state", "last_busy_activity_rows"),
    "_last_busy_activity_start_row": ("_render_state", "last_busy_activity_start_row"),
    "_panel_row_limit": ("_render_state", "panel_row_limit"),
    "_base_bottom_rows_cache_key": ("_render_state", "base_bottom_rows_cache_key"),
    "_base_bottom_rows_cache_count": ("_render_state", "base_bottom_rows_cache_count"),
    "_prev_frame_lines": ("_render_state", "prev_frame_lines"),
    "_prev_frame_start_row": ("_render_state", "prev_frame_start_row"),
    "_prev_frame_width": ("_render_state", "prev_frame_width"),
    "_prev_frame_term_height": ("_render_state", "prev_frame_term_height"),
    "_render_stats": ("_render_state", "render_stats"),
    "_external_request_handler": ("_external_state", "request_handler"),
    "_external_command_handler": ("_external_state", "command_handler"),
    "_mcp_catalog_provider": ("_external_state", "mcp_catalog_provider"),
    "_stdin_fd": ("_terminal_state", "stdin_fd"),
    "_tty": ("_terminal_state", "tty"),
    "_old_termios": ("_terminal_state", "old_termios"),
    "_windows_stdout_mode": ("_terminal_state", "windows_stdout_mode"),
    "_stdin_stream_reader": ("_terminal_state", "stdin_reader"),
    "_stdin_stream_transport": ("_terminal_state", "stdin_transport"),
    "_stdin_stream_pipe": ("_terminal_state", "stdin_pipe"),
    "_pending_bytes": ("_paste_state", "pending_bytes"),
    "_paste_buffer": ("_paste_state", "paste_buffer"),
    "_paste_entries": ("_paste_state", "entries"),
    "_paste_next_id": ("_paste_state", "next_id"),
    "_clipboard_change_count": ("_paste_state", "clipboard_change_count"),
}
