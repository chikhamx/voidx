"""Panel and transcript rendering mixin for PromptToolkitTui."""

from __future__ import annotations

from pathlib import Path

from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.formatted_text import AnyFormattedText
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from rich.cells import cell_len
from rich.markup import escape

from voidx.llm.usage import format_cache_hit_rate, format_token_count
from voidx.ui.code_ide import open_file_in_code_ide
from voidx.ui.app_components.formatting import (
    _args_preview,
    _clip,
    _continuation_prefix,
    _friendly_choice_label,
    _lines_to_formatted_text,
    _mcp_status_label,
    _permission_target,
    _visible_text,
)
from voidx.ui.app_components.file_picker import (
    AttachmentToken,
    FileCandidate,
    find_attachment_token,
    format_size,
    list_file_candidates,
)


def _candidate_meta(candidate: FileCandidate) -> str:
    if candidate.kind == "dir":
        items = candidate.size
        return f"  {candidate.kind} · {items} item{'s' if items != 1 else ''}"
    return f"  {candidate.kind} · {format_size(candidate.size)}"
from voidx.ui.dock import dock
from voidx.ui.session_changes import session_tracker

COMMAND_VISIBLE_ITEMS = 5
CHOICE_VISIBLE_ITEMS = 8
_EFFORT_LABELS = {
    "off": "关",
    "none": "关",
    "low": "低",
    "medium": "中",
    "high": "高",
    "xhigh": "超高",
}


class PromptToolkitRenderMixin:
    COMMAND_OUTPUT_WIDE_MIN = 110
    BOTTOM_BAR_HEIGHT = 4

    def _command_panel_active(self) -> bool:
        text = self.input.text
        return (
            self._active_choice is None
            and self._active_text_prompt is None
            and text.startswith("/")
            and text != self._command_panel_suppressed_text
        )

    def _attachment_panel_active(self) -> bool:
        text = self.input.text
        return (
            self._active_choice is None
            and self._active_text_prompt is None
            and not text.startswith("/")
            and text != self._attachment_panel_suppressed_text
            and self._attachment_token() is not None
        )

    def _attachment_token(self) -> AttachmentToken | None:
        return find_attachment_token(self.input.text, self.input.buffer.cursor_position)

    def _attachment_matches(self) -> list[FileCandidate]:
        token = self._attachment_token()
        if token is None:
            return []
        return list_file_candidates(self.status.workspace, token.query, limit=8)

    def _mcp_panel_active(self) -> bool:
        raw = self.input.text
        text = raw.strip().lower()
        if text == "/mcp":
            return True
        if not raw.lower().startswith("/mcp "):
            return False
        if text == "/mcp":
            return True
        mcp_cmds = [(n, d) for n, d in self.commands if n.lower().startswith("/mcp")]
        if any(name.lower() == text for name, _ in mcp_cmds):
            return False
        parts = text.split(None, 1)
        if len(parts) > 1 and not any(name.lower().startswith(text) for name, _ in mcp_cmds if " " in name):
            return True
        return False

    def _slash_matches(self) -> list[tuple[str, str]]:
        text = self.input.text.strip().lower()
        if not text or text == "/":
            return self.commands
        matched = [(name, desc) for name, desc in self.commands if name.lower().startswith(text)]
        if not matched:
            token = text.split(None, 1)[0]
            matched = [(name, desc) for name, desc in self.commands if name.lower().startswith(token)]
        return sorted(matched, key=lambda m: (" " in m[0], m[0]))

    def _mcp_servers(self) -> list[McpServerStatus]:
        try:
            return self.status.mcp_servers()
        except Exception:
            return []

    def _command_selectable_count(self) -> int:
        if self._mcp_panel_active():
            return min(len(self._mcp_servers()), 8)
        return min(len(self._slash_matches()), 8)

    def _attachment_selectable_count(self) -> int:
        return min(len(self._attachment_matches()), 8)

    def _clamp_command_selection(self) -> None:
        count = self._command_selectable_count()
        if count <= 0:
            self._command_selected = 0
            return
        self._command_selected = max(0, min(self._command_selected, count - 1))

    def _clamp_attachment_selection(self) -> None:
        count = self._attachment_selectable_count()
        if count <= 0:
            self._attachment_selected = 0
            return
        self._attachment_selected = max(0, min(self._attachment_selected, count - 1))

    def _move_command_selection(self, amount: int) -> None:
        count = self._command_selectable_count()
        if count <= 0:
            return
        self._command_selected = max(0, min(self._command_selected + amount, count - 1))

    def _move_command_selection_visual(self, direction: int) -> None:
        self._move_command_selection(-direction)

    def _move_attachment_selection(self, amount: int) -> None:
        count = self._attachment_selectable_count()
        if count <= 0:
            return
        self._attachment_selected = max(0, min(self._attachment_selected + amount, count - 1))

    def _accept_command_panel_selection(self) -> bool:
        if self._mcp_panel_active():
            return False

        matches = self._slash_matches()
        if not matches:
            return False

        selected = matches[min(self._command_selected, len(matches) - 1)][0]
        text = self.input.text.strip()
        if text == selected or text.startswith(selected + " "):
            return False

        self.input.text = selected
        self.input.buffer.cursor_position = len(selected)
        self._command_panel_suppressed_text = ""
        return True

    def _render_footer(self) -> AnyFormattedText:
        width = max(self._input_panel_width() - 3, 1)

        if self._active_choice is not None and self._choice_details:
            text = "  ↑/↓ select  Enter confirm  Esc cancel  a/y/n quick choose"
            return [("class:hints", text[:width])]

        if self._active_text_prompt is not None:
            detail = "input hidden" if self._active_text_secret else "text input"
            text = f"  Enter submit  Esc cancel  {detail}"
            return [("class:hints", text[:width])]

        if self._command_panel_active():
            text = "  ↑/↓ select  Enter confirm  Esc hide panel"
            return [("class:hints", text[:width])]

        if self._attachment_panel_active():
            text = "  ↑/↓ select  Enter attach  Esc hide panel"
            return [("class:hints", text[:width])]

        left = self._footer_left_fragment(width)
        left_text = left[1]
        status_fragments = self._status_fragments(max(width - len(left_text), 1))
        status_len = _fragment_text_len(status_fragments)
        available = max(width - len(left_text) - status_len, 0)

        positions: dict[str, int] = {"permission": 2}
        cursor = len(left_text) + available
        for text, _, anchor in self._status_segment_data(max(width - len(left_text), 1)):
            positions[anchor] = cursor
            cursor += len(text) + 2
        self._footer_anchor_positions = positions

        return [
            left,
            ("class:hints", " " * available),
            *status_fragments,
        ]

    def _status_text(self, width: int | None = None) -> str:
        provider = _safe_status_value(self.status.provider, "-")
        model = _safe_status_value(self.status.model, "-")
        effort = _safe_status_value(self.status.reasoning_effort, "xhigh")
        busy = " busy" if self._busy else ""
        error = f" error:{self._last_error[:32]}" if self._last_error else ""
        variants = [
            [f"{provider}/{model}", effort, f"{busy}{error}".strip()],
            [model, effort, f"{busy}{error}".strip()],
            [provider, f"{busy}{error}".strip()],
        ]
        if width is None:
            return _join_status_segments(variants[0])
        for segments in variants:
            text = _join_status_segments(segments)
            if len(text) <= width:
                return text
        return _clip(_join_status_segments(variants[-1]), width)

    def _footer_left_fragment(self, width: int) -> tuple:
        text = self.input.text
        if self._notice and not text:
            return ("class:hints", _clip("  " + self._notice, width))
        permission = _safe_status_value(self.status.permission_label(), "default")
        return (
            "class:footer.permission",
            _clip(f"  {permission}", width),
            self._footer_click_handler("/permission-mode", choice_anchor="permission"),
        )

    def _status_segment_data(self, width: int) -> list[tuple[str, str, str]]:
        provider = _safe_status_value(self.status.provider, "-")
        model = _safe_status_value(self.status.model, "-")
        effort = _safe_status_value(self.status.reasoning_effort, "xhigh")
        variants = [
            [
                (f"{provider}/{model}", "/model", "model"),
                (effort, "/model reasoning", "reasoning"),
            ],
            [
                (model, "/model", "model"),
                (effort, "/model reasoning", "reasoning"),
            ],
            [
                (provider, "/model", "provider"),
            ],
        ]
        for segments in variants:
            text = _join_status_segments([segment for segment, _, _ in segments])
            if len(text) <= width:
                return segments
        clipped = _clip(_join_status_segments([segment for segment, _, _ in variants[-1]]), width)
        return [(clipped, "/model", "status")]

    def _status_fragments(self, width: int) -> list[tuple]:
        return self._status_segment_fragments(self._status_segment_data(width))

    def _status_segment_fragments(self, segments: list[tuple[str, str, str]]) -> list[tuple]:
        fragments: list[tuple] = []
        for text, command, anchor in segments:
            if not text:
                continue
            if fragments:
                fragments.append(("class:hints", "  "))
            if command:
                fragments.append((
                    self._footer_status_style(anchor),
                    text,
                    self._footer_click_handler(command, choice_anchor=anchor),
                ))
            else:
                fragments.append(("class:hints", text))
        return fragments

    @staticmethod
    def _footer_status_style(anchor: str) -> str:
        if anchor == "reasoning":
            return "class:footer.reasoning"
        return "class:footer.model"

    def _footer_click_handler(self, command: str, *, quiet: bool = True, choice_anchor: str = ""):
        def _handler(mouse_event: MouseEvent) -> None:
            if mouse_event.event_type != MouseEventType.MOUSE_UP:
                return None
            if self._active_choice is not None:
                if self._choice_anchor == choice_anchor:
                    self._finish_choice(None)
                    self.invalidate()
                    return None
                self._finish_choice(None)
            self._command_panel_suppressed_text = ""
            self._attachment_panel_suppressed_text = ""
            self._choice_anchor = choice_anchor
            if choice_anchor == "permission":
                self._choice_current_value = _safe_status_value(self.status.permission_label(), "default")
            elif choice_anchor == "reasoning":
                self._choice_current_value = _safe_status_value(self.status.reasoning_effort, "xhigh")
            elif choice_anchor == "model":
                self._choice_current_value = _safe_status_value(self.status.model, "")
            if quiet:
                self.queue_quiet_command(command)
            else:
                self._queue.put_nowait(command)
            self.invalidate()
            return None

        return _handler

    def _render_detail_status_panel(self) -> AnyFormattedText:
        width = max(self._detail_status_width(), 20)
        rows: list[tuple[str, str]] = []
        stats = self.status.usage_stats
        context_limit = stats.context_limit or self.status.context_limit
        busy = "busy" if self._busy else "idle"
        if self._last_error:
            busy = f"error:{self._last_error[:20]}"

        state = busy
        detail_rows = [
            [
                ("class:status.label", "ctx "),
                (
                    "class:status.value",
                    f"{format_token_count(stats.context_tokens)}/{format_token_count(context_limit)}",
                ),
                ("class:status.dim", "  cache "),
                ("class:status.value", format_cache_hit_rate(stats)),
            ],
            [
                ("class:status.label", "calls "),
                ("class:status.value", format_token_count(stats.total_calls)),
                ("class:status.dim", "  "),
                ("class:status.label", "in "),
                ("class:status.value", format_token_count(stats.last_input_tokens)),
                ("class:status.dim", "  out "),
                ("class:status.value", format_token_count(stats.last_output_tokens)),
                ("class:status.dim", "  total "),
                ("class:status.value", format_token_count(stats.total_tokens)),
            ],
            [
                ("class:status.label", "s:"),
                ("class:status.value", _safe_status_value(self.status.sandbox_label(), "w-write")),
                ("class:status.dim", "  a:"),
                ("class:status.value", _safe_status_value(self.status.approval_label(), "on-fail")),
                ("class:status.dim", "  r:"),
                ("class:status.value", _safe_status_value(self.status.approval_reviewer_label(), "user")),
                ("class:status.dim", "  dbg:"),
                ("class:status.value", "on" if self.status.debug() else "off"),
            ],
            [
                ("class:status.label", "state:"),
                ("class:status.value", state),
                ("class:status.dim", "  mode:"),
                ("class:status.value", _safe_status_value(self.status.interaction_mode(), "auto")),
                ("class:status.dim", "  plan:"),
                ("class:status.value", "on" if self.status.plan_mode() else "off"),
            ],
        ]
        goal_label = _safe_status_value(self.status.goal_label(), "")
        goal_status = _safe_status_value(self.status.goal_status(), "idle")
        if goal_label or goal_status != "idle":
            approval = "waiting" if self.status.goal_awaiting_approval() else "none"
            detail_rows.append([
                ("class:status.label", "goal:"),
                ("class:status.value", _clip(goal_status, 12)),
                ("class:status.dim", "/"),
                ("class:status.value", _clip(_safe_status_value(self.status.goal_phase(), "clarify"), 14)),
                ("class:status.dim", "  turns "),
                ("class:status.value", str(self.status.goal_turn_count())),
                ("class:status.dim", "  approval:"),
                ("class:status.value", approval),
                ("class:status.dim", "  "),
                ("class:status.value", _clip(goal_label, max(12, width - 48))),
            ])
        for index, row in enumerate(detail_rows):
            self._append_status_line(rows, row, width, newline=index < len(detail_rows) - 1)
        return rows

    def _append_status_line(
        self,
        rows: list[tuple[str, str]],
        parts: list[tuple[str, str]],
        width: int,
        *,
        newline: bool = True,
    ) -> None:
        rows.append(("class:status", "  "))
        used = 2
        for style, text in parts:
            clipped = _clip(text, max(width - used, 0))
            rows.append((style, clipped))
            used += len(clipped)
        if used < width:
            rows.append(("class:status", " " * (width - used)))
        if newline:
            rows.append(("class:status", "\n"))

    def _render_body(self) -> AnyFormattedText:
        width = max(self._main_width() - 1, 20)
        lines, line_map = dock.tree.render_with_line_map(width)
        if not lines:
            lines = ["[dim]No conversation yet.[/]"]
            line_map = {}
        height = self._body_height()
        offset = min(self._scroll_offset, self._max_scroll(len(lines), height))
        end = len(lines) - offset
        start = max(0, end - height)
        visible = lines[start:end]
        visible_node_ids = [line_map.get(index) for index in range(start, end)]
        self._visible_body_lines = visible
        self._visible_body_node_ids = visible_node_ids

        # Build visual-row -> node_id mapping (accounts for line wrapping)
        # Uses width for first visual line, (width - prefix_w) for continuation
        # lines, matching prompt_toolkit's actual wrapping behaviour.
        row_map: dict[int, str | None] = {}
        visual_row = 0
        for i, line in enumerate(visible):
            vis_w = cell_len(_visible_text(line))
            prefix = _continuation_prefix(line)
            prefix_w = len(prefix)
            if vis_w <= width or prefix_w <= 0:
                wraps = max(1, (vis_w + width - 1) // width) if width > 0 else 1
            else:
                cont_width = max(width - prefix_w, 1)
                wraps = 1 + max(0, (vis_w - width + cont_width - 1) // cont_width)
            node_id = line_map.get(start + i)
            for row in range(visual_row, visual_row + wraps):
                row_map[row] = node_id
            visual_row += wraps
        self._visible_row_to_node = row_map

        return _lines_to_formatted_text(visible, width, follow_tail=offset == 0)

    def _render_choice_panel(self) -> AnyFormattedText:
        width = max(self._main_width() - 1, 32)
        if not self._choice_details:
            return self._render_compact_choice_panel()

        rows: list[tuple[str, str]] = []

        title = " Permission "
        top_fill = max(width - len(title) - 3, 0)
        rows.append(("class:permission.border", "╭─"))
        rows.append(("class:permission.title", title))
        rows.append(("class:permission.border", "─" * top_fill + "╮\n"))

        self._append_panel_line(rows, [("class:permission.prompt", self._choice_prompt)], width)

        details = self._choice_detail_lines()
        for line in details[:4]:
            self._append_panel_line(rows, line, width)
        if len(details) > 4:
            self._append_panel_line(rows, [("class:permission.dim", f"... +{len(details) - 4} more")], width)

        rows.append(("class:permission.border", "├" + "─" * (width - 2) + "┤\n"))
        choices = self._active_choice or []
        selected = max(0, min(self._choice_selected, len(choices) - 1))
        visible_count = min(len(choices), self._choice_visible_items())
        start, visible = _selected_window(choices, selected, visible_count)
        if start > 0:
            self._append_panel_line(rows, [("class:permission.dim", f"... {start} above")], width)
        for offset, (label, value, desc) in enumerate(visible):
            index = start + offset
            selected = index == self._choice_selected
            marker = "❯" if selected else " "
            text = _friendly_choice_label(label, value, desc)
            key = value if len(value) == 1 else ""
            style = "class:permission.choice.selected" if selected else "class:permission.choice"
            parts = [
                ("class:permission.marker", marker),
                (style, f" {text}"),
            ]
            if key:
                parts.append(("class:permission.key", f"  {key}"))
            self._append_panel_line(rows, parts, width)
        hidden_below = len(choices) - start - len(visible)
        if hidden_below > 0:
            self._append_panel_line(rows, [("class:permission.dim", f"... {hidden_below} below")], width)

        rows.append(("class:permission.border", "╰" + "─" * (width - 2) + "╯"))
        return rows

    def _render_compact_choice_panel(self) -> AnyFormattedText:
        menu_width = self._choice_float_width()
        rows: list[tuple[str, str]] = []
        choices = self._active_choice or []
        selected = max(0, min(self._choice_selected, len(choices) - 1))
        visible_count = min(len(choices), self._choice_visible_items())
        start, visible = _selected_window(choices, selected, visible_count)
        detail_lines = self._choice_detail_lines()
        left_pad = 0

        if self._choice_details:
            self._append_compact_choice_parts_row(
                rows,
                left_pad,
                menu_width,
                [("class:choice.prompt", self._choice_prompt)],
            )
            for line in detail_lines[:3]:
                self._append_compact_choice_parts_row(rows, left_pad, menu_width, line)
            if len(detail_lines) > 3:
                self._append_compact_choice_row(rows, left_pad, menu_width, f"... +{len(detail_lines) - 3} more", False, None)

        if start > 0:
            self._append_compact_choice_row(rows, left_pad, menu_width, f"... {start} above", False, None)

        for offset, (label, value, desc) in enumerate(visible):
            index = start + offset
            selected = index == self._choice_selected
            self._append_compact_choice_row(
                rows,
                left_pad,
                menu_width,
                _compact_choice_label(label, value, desc, self._choice_current_value),
                selected,
                index,
            )

        hidden_below = len(choices) - start - len(visible)
        if hidden_below > 0:
            self._append_compact_choice_row(rows, left_pad, menu_width, f"... {hidden_below} below", False, None)
        return rows

    def _append_compact_choice_row(
        self,
        rows: list[tuple[str, str]],
        left_pad: int,
        menu_width: int,
        label: str,
        selected: bool,
        index: int | None,
    ) -> None:
        rows.append(("class:choice.pad", " " * left_pad))
        text = "  " + _clip(label, max(menu_width - 3, 1))
        text += " " * max(menu_width - len(text), 0)
        style = "class:choice.selected" if selected else "class:choice"
        if index is None:
            rows.append((style, text))
        else:
            rows.append((style, text, self._compact_choice_click_handler(index)))
        rows.append(("class:choice.pad", "\n"))

    def _append_compact_choice_parts_row(
        self,
        rows: list[tuple[str, str]],
        left_pad: int,
        menu_width: int,
        parts: list[tuple[str, str]],
    ) -> None:
        rows.append(("class:choice.pad", " " * left_pad))
        rows.append(("class:choice", "  "))
        used = 2
        for style, text in parts:
            clipped = _clip(text, max(menu_width - used - 1, 0))
            rows.append((style, clipped))
            used += len(clipped)
        rows.append(("class:choice", " " * max(menu_width - used, 0)))
        rows.append(("class:choice.pad", "\n"))

    def _compact_choice_left_pad(self, width: int, menu_width: int) -> int:
        max_left = max(width - menu_width, 0)
        anchor = self._choice_anchor
        pos = self._footer_anchor_positions.get(anchor)
        if pos is not None:
            return max(0, min(pos, max_left))
        return max(width - menu_width - 2, 0)

    def _choice_float_width(self) -> int:
        width = self._choice_menu_width()
        choice_float = getattr(self, "_compact_choice_float", None)
        if choice_float is not None:
            choice_float.left = self._compact_choice_left_pad(
                max(self._input_panel_width() - 3, 1),
                width,
            )
        perm_float = getattr(self, "_permission_choice_float", None)
        if perm_float is not None:
            perm_float.left = max(self._input_panel_width() // 3, 2)
        return width

    def _choice_float_available_width(self) -> int:
        return max(self._input_panel_width() - 1, 32)

    def _choice_menu_width(self) -> int:
        width = self._choice_float_available_width()
        choices = self._active_choice or []
        selected = max(0, min(self._choice_selected, len(choices) - 1))
        visible_count = min(len(choices), self._choice_visible_items())
        _, visible = _selected_window(choices, selected, visible_count)
        labels = [_compact_choice_label(label, value, desc, self._choice_current_value) for label, value, desc in visible]
        content_width = max(
            [cell_len(label) for label in labels]
            + [cell_len(self._choice_prompt)]
            + [4]
        )
        result = min(content_width + 4, width)
        permission_float = getattr(self, "_permission_choice_float", None)
        if permission_float is not None and self._choice_details:
            permission_float.left = max(self._input_panel_width() // 3, 2)
        return result

    def _choice_anchor_for_prompt(self, prompt: str) -> str:
        normalized = prompt.strip().lower()
        if "permission" in normalized or "allow tool" in normalized:
            return "permission"
        if "effort" in normalized or "reasoning" in normalized:
            return "reasoning"
        if "provider" in normalized:
            return "provider"
        if "model" in normalized or "switch" in normalized:
            return "model"
        return ""

    def _compact_choice_click_handler(self, index: int):
        def _handler(mouse_event: MouseEvent) -> None:
            if mouse_event.event_type != MouseEventType.MOUSE_UP:
                return None
            choices = self._active_choice or []
            if index < 0 or index >= len(choices):
                return None
            self._choice_selected = index
            self._finish_choice(choices[index][1])
            self.invalidate()
            return None

        return _handler

    def _render_command_panel(self) -> AnyFormattedText:
        width = max(self._main_width() - 1, 32)
        if self._mcp_panel_active():
            return self._render_mcp_panel(width)
        return self._render_slash_panel(width)

    def _render_attachment_panel(self) -> AnyFormattedText:
        width = max(self._main_width() - 1, 32)
        rows: list[tuple[str, str]] = [("class:command.divider", "─" * width + "\n")]
        matches = self._attachment_matches()
        self._append_command_line(rows, [("class:command.title", "Attach files")], width)
        token = self._attachment_token()
        query = token.query if token is not None else ""
        detail = f"{len(matches)} match{'es' if len(matches) != 1 else ''}"
        if query:
            detail += f" for @{query}"
        self._append_command_line(rows, [("class:command.dim", detail)], width)

        if not matches:
            self._append_command_line(rows, [("class:command.dim", "No matching files")], width, indent="    ")
            return rows

        selected = min(self._attachment_selected, len(matches) - 1)
        visible_count = min(len(matches), COMMAND_VISIBLE_ITEMS)
        start, visible = _selected_window(matches, selected, visible_count)

        if start > 0:
            self._append_command_line(rows, [("class:command.dim", f"    ... {start} above")], width, indent="  ")

        for offset, candidate in enumerate(visible):
            index = start + offset
            marker = "❯" if index == selected else " "
            name_style = "class:command.selected" if index == selected else "class:command.name"
            meta = _candidate_meta(candidate)
            self._append_command_line(
                rows,
                [
                    ("class:command.marker", marker),
                    (name_style, f" {candidate.rel_path}"),
                    ("class:command.dim", meta),
                ],
                width,
                indent="  ",
            )

        hidden_below = len(matches) - start - len(visible)
        if hidden_below > 0:
            self._append_command_line(rows, [("class:command.dim", f"    ... {hidden_below} below")], width, indent="  ")

        return rows

    def _render_command_output_panel(self) -> AnyFormattedText:
        width = self.command_output_width()
        lines = self._command_output_lines if self._command_output_lines else []
        return _lines_to_formatted_text(lines, width, follow_tail=False)

    def _render_slash_panel(self, width: int) -> AnyFormattedText:
        rows: list[tuple[str, str]] = [("class:command.divider", "─" * width + "\n")]
        matches = self._slash_matches()
        self._append_command_line(rows, [("class:command.title", "Slash commands")], width)
        count = f"{len(matches)} command{'s' if len(matches) != 1 else ''}"
        self._append_command_line(rows, [("class:command.dim", count)], width)

        if not matches:
            self._append_command_line(rows, [("class:command.dim", "No matching commands")], width, indent="    ")
            return rows

        selected = self._command_selected
        visible_count = min(len(matches), COMMAND_VISIBLE_ITEMS)
        start, visible = _selected_window(matches, selected, visible_count)

        if start > 0:
            self._append_command_line(rows, [("class:command.dim", f"    ... {start} above")], width, indent="  ")

        for offset, (name, desc) in enumerate(reversed(visible)):
            original_index = start + len(visible) - 1 - offset
            marker = "❯" if original_index == selected else " "
            command_style = "class:command.selected" if original_index == selected else "class:command.name"
            self._append_command_line(
                rows,
                [
                    ("class:command.marker", marker),
                    (command_style, f" {name}"),
                    ("class:command.dim", f"  {desc}"),
                ],
                width,
                indent="  ",
            )

        hidden_below = len(matches) - start - len(visible)
        if hidden_below > 0:
            self._append_command_line(rows, [("class:command.dim", f"    ... {hidden_below} below")], width, indent="  ")

        return rows

    def _render_mcp_panel(self, width: int) -> AnyFormattedText:
        rows: list[tuple[str, str]] = [("class:command.divider", "─" * width + "\n")]
        servers = self._mcp_servers()
        self._append_command_line(rows, [("class:command.title", "Manage MCP servers")], width)
        count = f"{len(servers)} server{'s' if len(servers) != 1 else ''}"
        self._append_command_line(rows, [("class:command.dim", count)], width)
        self._append_command_line(rows, [], width)

        if not servers:
            self._append_command_line(
                rows,
                [("class:command.dim", "No MCP servers configured")],
                width,
                indent="    ",
            )
            return rows

        source = servers[0].source if servers else "Project MCPs"
        full = self.status.mcp_config_path or f"{self.status.workspace}/voidx.json"
        try:
            path = str(Path(full).resolve().relative_to(Path(self.status.workspace).resolve()))
        except ValueError:
            path = full
        self._append_command_line(
            rows,
            [
                ("class:command.group", source),
                ("class:command.dim", f" ({path})"),
            ],
            width,
            indent="    ",
        )

        selected = self._command_selected
        visible_count = min(len(servers), COMMAND_VISIBLE_ITEMS)
        start, visible = _selected_window(servers, selected, visible_count)

        if start > 0:
            self._append_command_line(rows, [("class:command.dim", f"    ... {start} above")], width, indent="  ")

        for offset, server in enumerate(reversed(visible)):
            original_index = start + len(visible) - 1 - offset
            marker = "❯" if original_index == selected else " "
            status = _mcp_status_label(server.status)
            tools = f"{server.tool_count} tool{'s' if server.tool_count != 1 else ''}"
            name_style = "class:command.selected" if original_index == selected else "class:command.name"
            self._append_command_line(
                rows,
                [
                    ("class:command.marker", marker),
                    (name_style, f" {server.name}"),
                    ("class:command.dim", " · "),
                    (status[0], status[1]),
                    ("class:command.dim", f" · {tools}"),
                ],
                width,
                indent="  ",
            )

        hidden_below = len(servers) - start - len(visible)
        if hidden_below > 0:
            self._append_command_line(rows, [("class:command.dim", f"    ... {hidden_below} below")], width, indent="  ")

        return rows

    def _append_command_line(
        self,
        rows: list[tuple[str, str]],
        parts: list[tuple[str, str]],
        width: int,
        *,
        indent: str = "  ",
    ) -> None:
        rows.append(("class:command", indent))
        used = len(indent)
        for style, text in parts:
            clipped = _clip(text, max(width - used, 0))
            rows.append((style, clipped))
            used += len(clipped)
        rows.append(("class:command", "\n"))

    def _choice_detail_lines(self) -> list[list[tuple[str, str]]]:
        lines: list[list[tuple[str, str]]] = []
        for detail in self._choice_details:
            name = str(detail.get("name") or "tool")
            pattern = str(detail.get("pattern") or "")
            args = detail.get("args") if isinstance(detail.get("args"), dict) else {}
            target = pattern if pattern and pattern != "*" else _permission_target(args)
            lines.append([
                ("class:choice.tool", name),
                ("class:choice.dim", f"  {target}" if target else ""),
            ])
            preview = _args_preview(args)
            if preview:
                lines.append([("class:choice.dim", f"  {preview}")])
        return lines

    def _append_panel_line(
        self,
        rows: list[tuple[str, str]],
        parts: list[tuple[str, str]],
        width: int,
    ) -> None:
        rows.append(("class:permission.border", "│ "))
        used = 2
        for style, text in parts:
            clipped = _clip(text, max(width - used - 2, 0))
            rows.append((style, clipped))
            used += len(clipped)
        rows.append(("class:permission", " " * max(width - used - 1, 0)))
        rows.append(("class:permission.border", "│\n"))

    def _body_line_prefix(self, line_number: int, wrap_count: int) -> AnyFormattedText:
        if wrap_count <= 0 or line_number >= len(self._visible_body_lines):
            return []
        prefix = _continuation_prefix(self._visible_body_lines[line_number])
        return [("", prefix)] if prefix else []

    def _hint_text(self) -> str:
        text = self.input.text
        if self._notice and not text:
            return self._notice
        if not text.startswith("/"):
            return "wheel/click transcript · @ attach · ^V image"
        p = text.lower()
        matches = [(name, desc) for name, desc in self.commands if name.lower().startswith(p)]
        if not matches:
            return "no matching commands"
        shown = "  ".join(f"{name} {desc}" for name, desc in matches[:4])
        return shown

    def _body_height(self) -> int:
        rows = 24
        app = get_app_or_none()
        if app is not None:
            rows = app.output.get_size().rows
        bottom_rows = self._bottom_bar_height()
        choice_rows = 0
        command_rows = self._command_panel_height() if self._command_panel_active() else 0
        attachment_rows = self._attachment_panel_height() if self._attachment_panel_active() else 0
        gap_rows = self._transcript_bottom_gap_height()
        return max(rows - 1 - choice_rows - command_rows - attachment_rows - bottom_rows - gap_rows - 1, 1)

    def _bottom_bar_height(self) -> int:
        return self.BOTTOM_BAR_HEIGHT

    def _transcript_bottom_gap_height(self) -> int:
        return 1

    def _input_panel_width(self) -> int:
        available = max(self._main_width() - 1, 1)
        return max(1, available - self._detail_status_width())

    def _detail_status_width(self) -> int:
        available = max(self._main_width() - 1, 1)
        input_width = (available * 3) // 5
        detail_width = available - input_width
        if available >= 150:
            detail_width = max(detail_width, 72)
        return max(1, min(detail_width, available - 1))

    def _choice_visible_items(self) -> int:
        return 3 if self._choice_details else CHOICE_VISIBLE_ITEMS

    def _choice_panel_height(self) -> int:
        if self._active_choice is None:
            return 0
        choices = self._active_choice or []
        visible_count = min(len(choices), self._choice_visible_items())
        selected = max(0, min(self._choice_selected, len(choices) - 1))
        start, visible = _selected_window(choices, selected, visible_count)
        indicator_rows = int(start > 0) + int(len(choices) - start - len(visible) > 0)
        detail_rows = 0
        if self._choice_details:
            details = self._choice_detail_lines()
            detail_rows = 1 + min(len(details), 3) + int(len(details) > 3)
        return min(16, max(1, detail_rows + visible_count + indicator_rows))

    def _command_panel_height(self) -> int:
        if self._mcp_panel_active():
            servers = self._mcp_servers()
            visible = min(len(servers), COMMAND_VISIBLE_ITEMS)
            return 1 + visible + (2 if len(servers) > visible else 0)
        matches = self._slash_matches()
        visible = min(len(matches), COMMAND_VISIBLE_ITEMS)
        return 1 + visible + (2 if len(matches) > visible else 0)

    def _attachment_panel_height(self) -> int:
        matches = self._attachment_matches()
        visible = min(len(matches), COMMAND_VISIBLE_ITEMS)
        return 3 + visible + (2 if len(matches) > visible else 0)

    def _line_count(self) -> int:
        return len(dock.tree.render(self._main_width())) or 1

    def _max_scroll(self, line_count: int | None = None, height: int | None = None) -> int:
        line_count = self._line_count() if line_count is None else line_count
        height = self._body_height() if height is None else height
        return max(line_count - height, 0)

    def _scroll_by(self, amount: int) -> None:
        self._scroll_offset = max(0, min(self._scroll_offset + amount, self._max_scroll()))

    def _toggle_body_node_at(self, row: int) -> None:
        node_id = self._visible_row_to_node.get(row)
        if not node_id:
            return
        node = dock.tree.get(node_id)
        if node is None or not (node.body_lines or node.children):
            return
        node.collapsed = not node.collapsed
        dock.tree.mark_dirty()

    def _scroll_to_top(self) -> None:
        self._scroll_offset = self._max_scroll()

    def _scroll_to_bottom(self) -> None:
        self._scroll_offset = 0

    def _render_scrollbar_margin(self, height: int) -> list[tuple[str, str]]:
        line_count = self._line_count()
        max_scroll = max(line_count - height, 0)
        if line_count <= height or height <= 0:
            return [("class:scrollbar.background", " \n") for _ in range(height)]

        thumb_height = max(1, min(height, int(height * height / line_count)))
        max_top = max(height - thumb_height, 0)
        if max_scroll:
            position = 1 - (self._scroll_offset / max_scroll)
            thumb_top = round(max_top * position)
        else:
            thumb_top = max_top

        result: list[tuple[str, str]] = []
        for row in range(height):
            in_thumb = thumb_top <= row < thumb_top + thumb_height
            style = "class:scrollbar.button" if in_thumb else "class:scrollbar.background"
            result.append((style, " "))
            if row < height - 1:
                result.append(("", "\n"))
        return result

    def _width(self) -> int:
        app = get_app_or_none()
        if app is not None:
            return max(app.output.get_size().columns, 20)
        return 80

    def _main_width(self) -> int:
        return self._width()

    def _command_output_float_width(self) -> int:
        available = max(self._width() - 4, 20)
        return min(max(36, self._width() // 3), available)

    def _command_output_float_height(self) -> int:
        return max(3, min(18, self._body_height()))

    def _command_output_active(self) -> bool:
        return bool(self._command_output_visible and self._command_output_lines)

    def _command_output_wide_active(self) -> bool:
        return False

    def _command_output_bottom_active(self) -> bool:
        return False

    def _review_panel_active(self) -> bool:
        return getattr(self, '_review_active', False)

    def _render_changes_bar(self) -> AnyFormattedText:
        full_width = max(self._main_width() - 4, 20)
        count = session_tracker.file_count
        added = session_tracker.total_added
        removed = session_tracker.total_removed

        label = f"{count} file{'s' if count != 1 else ''} changed this turn"
        added_str = f"+{added}"
        removed_str = f"\u2212{removed}"
        review_btn = " Review "

        full_text = f"  {label}    {added_str}  {removed_str}  {review_btn}  "
        content_len = len(full_text)
        pad_left = max((full_width - content_len) // 2, 0)
        pad_right = max(full_width - pad_left - content_len, 0)

        result: list = [("class:body", " " * pad_left)]
        result.append(("class:changes", "  "))
        result.append(("class:changes.label", label))
        result.append(("class:changes.dim", "    "))
        result.append(("class:changes.added", added_str))
        result.append(("class:changes.dim", "  "))
        result.append(("class:changes.removed", removed_str))
        result.append(("class:changes.dim", "  "))
        result.append(("class:changes.review", review_btn, self._review_click_handler()))
        result.append(("class:changes", "  "))
        result.append(("class:body", " " * pad_right))
        return result

    def _render_review_panel(self) -> AnyFormattedText:
        full_width = max(self._main_width() - 4, 20)
        files = session_tracker.files
        added = session_tracker.total_added
        removed = session_tracker.total_removed
        count = len(files)

        header = f"Turn changes: {count} file{'s' if count != 1 else ''}  +{added}  \u2212{removed}"
        rollback_btn = " Rollback all "
        hint = "Esc to close  |  Click file to open"

        lines: list[list[tuple]] = []
        lines.append([
            ("class:changes.label", f"  {header}  "),
            ("class:changes.rollback", rollback_btn, self._rollback_click_handler()),
        ])

        if not files:
            lines.append([("class:changes.dim", "  No changes  ")])
        else:
            for f in files[:12]:
                a_str = f"+{f.added}"
                d_str = f"\u2212{f.removed}"
                lines.append([
                    ("class:changes", "  "),
                    ("class:review.file", f.path, self._file_click_handler(f.path)),
                    ("class:review.dim", "  "),
                    ("class:review.added", a_str),
                    ("class:review.dim", "  "),
                    ("class:review.removed", d_str),
                    ("class:changes", "  "),
                ])

        lines.append([("class:changes.dim", f"  {hint}  ")])

        block_width = max(_fragment_text_len(line) for line in lines)
        pad = max((full_width - block_width) // 2, 0)

        result: list = [("class:body", "\n")]
        for line in lines:
            line_width = _fragment_text_len(line)
            right_pad = max(block_width - line_width, 0)
            result.append(("class:body", " " * pad))
            result.extend(line)
            if right_pad:
                result.append(("class:changes", " " * right_pad))
            result.append(("class:body", "\n"))
        return result

    def _review_panel_height(self) -> int:
        files = session_tracker.files
        return min(len(files) + 4, 15)

    def _review_click_handler(self):
        def _handler(mouse_event: MouseEvent) -> None:
            if mouse_event.event_type != MouseEventType.MOUSE_UP:
                return None
            self._review_active = not self._review_active
            self.invalidate()
            return None

        return _handler

    def _file_click_handler(self, file_path: str):
        def _handler(mouse_event: MouseEvent) -> None:
            if mouse_event.event_type != MouseEventType.MOUSE_UP:
                return None
            workspace = self.status.workspace
            full = Path(workspace) / file_path
            opened = open_file_in_code_ide(
                full,
                line=1,
                preferred=self.status.code_ide(),
            )
            self._notice = f"Opened {file_path}" if opened else f"Could not open {file_path}. Use /code-ide status."
            self._review_active = False
            self.invalidate()
            return None

        return _handler

    def _rollback_click_handler(self):
        def _handler(mouse_event: MouseEvent) -> None:
            if mouse_event.event_type != MouseEventType.MOUSE_UP:
                return None
            result = session_tracker.rollback_current()
            self._review_active = False
            if result.ok:
                restored = len(result.restored)
                removed = len(result.removed)
                parts = []
                if restored:
                    parts.append(f"restored {restored}")
                if removed:
                    parts.append(f"removed {removed}")
                self._notice = "Rollback complete" + (f": {', '.join(parts)}" if parts else "")
            else:
                self._notice = "Rollback failed: " + "; ".join(result.errors[:2])
            self.invalidate()
            return None

        return _handler


def _selected_window(items: list, selected: int, size: int) -> tuple[int, list]:
    if size <= 0 or not items:
        return 0, []
    selected = max(0, min(selected, len(items) - 1))
    half = size // 2
    start = max(0, selected - half)
    start = min(start, max(len(items) - size, 0))
    end = min(start + size, len(items))
    return start, items[start:end]


def _join_status_segments(segments: list[str]) -> str:
    return "  ".join(segment for segment in segments if segment)


def _fragment_text(parts: list[tuple[str, str]]) -> str:
    return "".join(text for _, text in parts)


def _safe_status_value(value: object, fallback: str) -> str:
    text = str(value or "").strip()
    return text if text else fallback


def _fragment_text_len(fragments: list[tuple]) -> int:
    return sum(len(fragment[1]) for fragment in fragments)


def _effort_label(value: str) -> str:
    return value


def _compact_choice_label(label: str, value: str, desc: str, current: str = "") -> str:
    if value in ("a", "y", "n"):
        return _friendly_choice_label(label, value, desc)
    prefix = "✓ " if current and (current == value or current == label) else ""
    return prefix + label
