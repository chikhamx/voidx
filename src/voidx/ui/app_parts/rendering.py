"""Panel and transcript rendering mixin for PromptToolkitTui."""

from __future__ import annotations

from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.formatted_text import AnyFormattedText
from rich.markup import escape

from voidx.ui.app_parts.formatting import (
    _args_preview,
    _clip,
    _continuation_prefix,
    _friendly_choice_label,
    _lines_to_formatted_text,
    _mcp_status_label,
    _permission_target,
)
from voidx.ui.app_parts.file_picker import (
    AttachmentToken,
    FileCandidate,
    find_attachment_token,
    format_size,
    list_file_candidates,
)
from voidx.ui.dock import dock

COMMAND_VISIBLE_ITEMS = 5
CHOICE_VISIBLE_ITEMS = 8


class PromptToolkitRenderMixin:
    COMMAND_OUTPUT_WIDE_MIN = 110

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
        parts = self.input.text.strip().split(None, 1)
        if not parts:
            return False
        token = parts[0].lower()
        return token == "/mcp"

    def _slash_matches(self) -> list[tuple[str, str]]:
        text = self.input.text.strip().lower()
        if not text or text == "/":
            return self.commands
        token = text.split(None, 1)[0]
        return [(name, desc) for name, desc in self.commands if name.lower().startswith(token)]

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
        self._command_selected = (self._command_selected + amount) % count

    def _move_attachment_selection(self, amount: int) -> None:
        count = self._attachment_selectable_count()
        if count <= 0:
            return
        self._attachment_selected = (self._attachment_selected + amount) % count

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
        width = max(self._main_width() - 1, 1)

        if self._active_choice is not None:
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

        if self._command_output_active():
            text = "  Esc close command output"
            if self._notice:
                text = "  " + self._notice
            status = self._status_text()
            available = max(width - len(text) - len(status), 1)
            return [
                ("class:hints", text[:width]),
                ("class:hints", " " * available),
                ("class:hints", status[: max(width - len(text), 0)]),
            ]

        hint = "  " + self._hint_text()
        status = self._status_text()
        gap = 2
        if len(hint) + gap + len(status) > width:
            hint_budget = max(width - gap - len(status), 0)
            if hint_budget <= 3:
                hint = ""
            else:
                hint = hint[: hint_budget - 3].rstrip() + "..."
        if len(hint) + gap + len(status) > width:
            status_budget = max(width - len(hint) - gap, 0)
            status = status[:status_budget]
        available = max(width - len(hint) - len(status), 1)
        return [
            ("class:hints", hint),
            ("class:hints", " " * available),
            ("class:hints", status),
        ]

    def _status_text(self) -> str:
        context = self._format_context()
        mode = "plan:on" if self.status.plan_mode() else "plan:off"
        debug = "debug:on" if self.status.debug() else "debug:off"
        busy = "busy" if self._busy else "idle"
        error = f"  error:{self._last_error[:40]}" if self._last_error else ""
        return f"{context}  {debug}  {mode}  {busy}{error}"

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
        return _lines_to_formatted_text(visible, width, follow_tail=offset == 0)

    def _render_choice_panel(self) -> AnyFormattedText:
        width = max(self._main_width() - 1, 32)
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
        start, visible = _selected_window(matches, selected, COMMAND_VISIBLE_ITEMS)
        for offset, candidate in enumerate(visible):
            index = start + offset
            marker = "❯" if index == selected else " "
            name_style = "class:command.selected" if index == selected else "class:command.name"
            meta = f"  {candidate.kind} · {format_size(candidate.size)}"
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
        return rows

    def _render_command_output_panel(self) -> AnyFormattedText:
        width = self.command_output_width()
        title = self._command_output_title or "Command output"
        lines = [
            "[bold #B7C1FF]Command Output[/bold #B7C1FF]",
            f"[dim]{escape(title)}[/dim]",
            "",
        ]
        if self._command_output_lines:
            lines.extend(self._command_output_lines)
        else:
            lines.append("[dim]No output.[/dim]")
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

        selected = min(self._command_selected, len(matches) - 1)
        for index, (name, desc) in enumerate(matches[:8]):
            marker = "❯" if index == selected else " "
            command_style = "class:command.selected" if index == selected else "class:command.name"
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
        return rows

    def _render_mcp_panel(self, width: int) -> AnyFormattedText:
        rows: list[tuple[str, str]] = [("class:command.divider", "─" * width + "\n")]
        servers = self._mcp_servers()
        self._append_command_line(rows, [("class:command.title", "Manage MCP servers")], width)
        count = f"{len(servers)} server{'s' if len(servers) != 1 else ''}"
        self._append_command_line(rows, [("class:command.dim", count)], width)
        self._append_command_line(rows, [], width)

        source = servers[0].source if servers else "Project MCPs"
        path = self.status.mcp_config_path or f"{self.status.workspace}/voidx.json"
        self._append_command_line(
            rows,
            [
                ("class:command.group", source),
                ("class:command.dim", f" ({path})"),
            ],
            width,
            indent="    ",
        )

        if not servers:
            self._append_command_line(
                rows,
                [("class:command.dim", "No MCP servers configured")],
                width,
                indent="    ",
            )
            return rows

        selected = min(self._command_selected, len(servers) - 1)
        for index, server in enumerate(servers[:8]):
            marker = "❯" if index == selected else " "
            status = _mcp_status_label(server.status)
            tools = f"{server.tool_count} tool{'s' if server.tool_count != 1 else ''}"
            name_style = "class:command.selected" if index == selected else "class:command.name"
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
                ("class:permission.tool", name),
                ("class:permission.dim", f"  {target}" if target else ""),
            ])
            preview = _args_preview(args)
            if preview:
                lines.append([("class:permission.dim", f"  {preview}")])
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

    def _format_context(self) -> str:
        limit = self.status.context_limit
        if limit >= 1000:
            limit_text = f"{limit // 1000}k"
        else:
            limit_text = str(limit)
        return f"ctx --/{limit_text}  in --  out --  cost --"

    def _body_height(self) -> int:
        rows = 24
        app = get_app_or_none()
        if app is not None:
            rows = app.output.get_size().rows
        input_rows = 2
        choice_rows = self._choice_panel_height()
        command_rows = self._command_panel_height() if self._command_panel_active() else 0
        attachment_rows = self._attachment_panel_height() if self._attachment_panel_active() else 0
        output_rows = self._command_output_panel_height() if self._command_output_bottom_active() else 0
        return max(rows - 1 - choice_rows - command_rows - attachment_rows - output_rows - input_rows - 1 - 1, 1)

    def _choice_visible_items(self) -> int:
        return 3 if self._choice_details else CHOICE_VISIBLE_ITEMS

    def _choice_panel_height(self) -> int:
        if self._active_choice is None:
            return 0
        details = self._choice_detail_lines()
        detail_rows = min(len(details), 4)
        if len(details) > 4:
            detail_rows += 1
        choices = self._active_choice or []
        visible_count = min(len(choices), self._choice_visible_items())
        selected = max(0, min(self._choice_selected, len(choices) - 1))
        start, visible = _selected_window(choices, selected, visible_count)
        indicator_rows = int(start > 0) + int(len(choices) - start - len(visible) > 0)
        return min(16, 4 + detail_rows + visible_count + indicator_rows)

    def _command_panel_height(self) -> int:
        if self._mcp_panel_active():
            return min(12, 5 + max(len(self._mcp_servers()), 1))
        return min(12, 3 + max(len(self._slash_matches()), 1))

    def _attachment_panel_height(self) -> int:
        return min(12, 3 + max(len(self._attachment_matches()), 1))

    def _command_output_panel_height(self) -> int:
        return min(12, 3 + max(len(self._command_output_lines), 1))

    def _line_count(self) -> int:
        return len(dock.tree.render(self._main_width())) or 1

    def _max_scroll(self, line_count: int | None = None, height: int | None = None) -> int:
        line_count = self._line_count() if line_count is None else line_count
        height = self._body_height() if height is None else height
        return max(line_count - height, 0)

    def _scroll_by(self, amount: int) -> None:
        self._scroll_offset = max(0, min(self._scroll_offset + amount, self._max_scroll()))

    def _toggle_body_node_at(self, row: int) -> None:
        if row < 0 or row >= len(self._visible_body_node_ids):
            return
        node_id = self._visible_body_node_ids[row]
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
        width = self._width()
        if self._command_output_wide_active():
            return max(width - self._command_output_side_width() - 1, 20)
        return width

    def _command_output_side_width(self) -> int:
        return max(36, min(50, self._width() // 3))

    def _command_output_wide_possible(self) -> bool:
        return self._width() >= self.COMMAND_OUTPUT_WIDE_MIN

    def _command_output_active(self) -> bool:
        return bool(self._command_output_visible and self._command_output_lines)

    def _command_output_wide_active(self) -> bool:
        return self._command_output_active() and self._command_output_wide_possible()

    def _command_output_bottom_active(self) -> bool:
        return self._command_output_active() and not self._command_output_wide_active()


def _selected_window(items: list, selected: int, size: int) -> tuple[int, list]:
    if size <= 0 or not items:
        return 0, []
    selected = max(0, min(selected, len(items) - 1))
    half = size // 2
    start = max(0, selected - half)
    start = min(start, max(len(items) - size, 0))
    end = min(start + size, len(items))
    return start, items[start:end]
