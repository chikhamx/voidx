"""Panel management — command palette, attachment panel, choice overlay."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from voidx.config import Settings
from voidx.paths import voidx_workspace_dir
from voidx.skills.service import SkillRegistry, SkillService
from voidx.ui.output.dock.formatting import _PASTED_RE
from voidx.ui.tools.attachment_tokens import attachment_token_text
from voidx.ui.tools.file_picker import (
    AttachmentToken,
    FileCandidate,
    find_attachment_token,
    list_file_candidates,
)
from voidx.ui.tools.skill_picker import (
    SkillCandidate,
    SkillToken,
    find_skill_token,
    list_skill_candidates,
)
from voidx.ui.tools.mcp_picker import (
    McpCandidate,
    list_mcp_candidates,
)


class _PanelManagerMixin:
    """Methods: _update_input_panels, _update_command_panel, _attachment_token,
    _attachment_panel_active, _attachment_matches, _attachment_selectable_count,
    _skill_token, _skill_panel_active, _skill_matches,
    _skill_selectable_count, _clamp_attachment_selection,
    _clamp_skill_selection, _filtered_commands, _move_command_selection,
    _move_attachment_selection, _move_skill_selection,
    _accept_command_panel_selection, _accept_attachment_panel_selection,
    _accept_skill_panel_selection, _move_choice, _finish_choice,
    _submit_choice_selection, _submit_text_prompt, _cancel_text_prompt,
    _handle_escape, _handle_tab, _normalize_choice_detail."""

    # ── submit ───────────────────────────────────────────────────────────

    def _submit_text_prompt(self) -> None:
        value = self._expand_registered_tokens(self._get_input_text())
        # Clarify answers are plain text — strip <pasted> wrapper tags that
        # _expand_registered_tokens adds (those tags are only consumed by the
        # main input path's split_pasted_segments in start_turn rendering).
        value = _PASTED_RE.sub(r"\1", value)
        self._paste_entries.clear()
        self._text_queue.put_nowait(value)

    def _cancel_text_prompt(self) -> None:
        self._text_queue.put_nowait(None)

    def _handle_escape(self) -> None:
        if self._active_text_prompt is not None:
            self._cancel_text_prompt()
        elif self._active_choice is not None:
            self._finish_choice(None)
        elif self._skill_panel_active():
            self._skill_panel_suppressed_text = self._get_input_text()
        elif self._attachment_panel_active():
            self._attachment_panel_suppressed_text = self._get_input_text()
        elif self._command_panel_active:
            self._command_panel_active = False
        self.invalidate()

    def _handle_tab(self) -> None:
        """Tab completion for commands and slash panel."""
        if self._skill_panel_active():
            self._accept_skill_panel_selection()
            return
        if self._attachment_panel_active():
            self._accept_attachment_panel_selection()
            return
        line = self._current_line()
        if line.startswith("/"):
            self._reset_ctrl_c()
            filtered = self._filtered_commands()
            if len(filtered) == 1:
                self._input_lines[self._cursor_row] = filtered[0][0]
                self._cursor_col = len(self._input_lines[self._cursor_row])
                self._command_panel_active = False
            elif len(filtered) > 1:
                # Find common prefix
                common = filtered[0][0]
                for name, _ in filtered[1:]:
                    while not name.startswith(common):
                        common = common[:-1]
                if len(common) > len(line):
                    self._input_lines[self._cursor_row] = common
                    self._cursor_col = len(common)
            self.invalidate()

    # ── panel state ──────────────────────────────────────────────────────

    def _update_input_panels(self) -> None:
        self._update_command_panel()
        self._clamp_attachment_selection()
        self._clamp_skill_selection()

    def _update_command_panel(self) -> None:
        line = self._current_line()
        if self._cursor_row == len(self._input_lines) - 1 and line.startswith("/"):
            if find_attachment_token(self._get_input_text(), self._input_cursor_position()) is not None:
                self._command_panel_active = False
            else:
                self._command_panel_active = True
                self._command_selected = 0
        else:
            self._command_panel_active = False

    def _attachment_token(self) -> AttachmentToken | None:
        if self._active_choice is not None or self._active_text_prompt is not None:
            return None
        if self._command_panel_active:
            return None
        return find_attachment_token(self._get_input_text(), self._input_cursor_position())

    def _skill_token(self) -> SkillToken | None:
        if self._active_choice is not None or self._active_text_prompt is not None:
            return None
        if self._command_panel_active:
            return None
        return find_skill_token(self._get_input_text(), self._input_cursor_position())

    def _attachment_panel_active(self) -> bool:
        text = self._get_input_text()
        return (
            self._active_choice is None
            and self._active_text_prompt is None
            and not self._command_panel_active
            and text != self._attachment_panel_suppressed_text
            and self._attachment_token() is not None
        )

    def _skill_panel_active(self) -> bool:
        text = self._get_input_text()
        return (
            self._active_choice is None
            and self._active_text_prompt is None
            and not self._command_panel_active
            and text != self._skill_panel_suppressed_text
            and self._skill_token() is not None
        )

    def _attachment_matches(self) -> list[FileCandidate]:
        token = self._attachment_token()
        if token is None:
            self._attachment_matches_cache_key = None
            self._attachment_matches_cache = []
            return []
        workspace = str(self.status.workspace)
        key = (workspace, token.query, token.start, token.end)
        if key == self._attachment_matches_cache_key:
            return self._attachment_matches_cache
        matches = list_file_candidates(workspace, token.query, limit=8)
        self._attachment_matches_cache_key = key
        self._attachment_matches_cache = matches
        return matches

    def _skill_matches(self) -> list[SkillCandidate]:
        token = self._skill_token()
        if token is None:
            self._skill_matches_cache_key = None
            self._skill_matches_cache = []
            return []
        workspace = str(self.status.workspace)
        key = (workspace, token.query, token.start, token.end)
        if key == self._skill_matches_cache_key:
            return self._skill_matches_cache
        skill_matches = list_skill_candidates(
            workspace,
            token.query,
            limit=8,
            service=self._skill_candidate_service(workspace),
        )
        mcp_catalog = self._mcp_catalog_provider() if self._mcp_catalog_provider else None
        mcp_matches = list_mcp_candidates(
            workspace, token.query, limit=8, catalog=mcp_catalog,
        )
        mcp_as_skill = [
            SkillCandidate(name=m.name, scope="mcp", description=m.description, mode=m.mode)
            for m in mcp_matches
        ]
        matches = [*skill_matches, *mcp_as_skill][:8]
        self._skill_matches_cache_key = key
        self._skill_matches_cache = matches
        return matches

    def _skill_candidate_service(self, workspace: str) -> SkillService:
        key = (workspace, *_skill_settings_signature(workspace))
        if key == self._skill_service_cache_key and self._skill_service_cache is not None:
            return self._skill_service_cache
        settings = Settings(workspace)
        service = SkillService(
            SkillRegistry(workspace),
            selection=settings.get_skill_selection(),
        )
        self._skill_service_cache_key = key
        self._skill_service_cache = service
        return service

    def _attachment_selectable_count(self) -> int:
        return min(len(self._attachment_matches()), 8)

    def _skill_selectable_count(self) -> int:
        return min(len(self._skill_matches()), 8)

    def _clamp_attachment_selection(self) -> None:
        count = self._attachment_selectable_count()
        if count <= 0:
            self._attachment_selected = 0
            return
        self._attachment_selected = max(0, min(self._attachment_selected, count - 1))

    def _clamp_skill_selection(self) -> None:
        count = self._skill_selectable_count()
        if count <= 0:
            self._skill_selected = 0
            return
        self._skill_selected = max(0, min(self._skill_selected, count - 1))

    def _filtered_commands(self) -> list[tuple[str, str]]:
        line = self._current_line().strip()
        if not line.startswith("/"):
            return []
        p = line.lower()
        return [(n, d) for n, d in self.commands if n.lower().startswith(p)]

    def _move_command_selection(self, delta: int) -> None:
        count = min(len(self._filtered_commands()), 8)
        if count <= 0:
            self._command_selected = 0
            return
        self._command_selected = max(0, min(self._command_selected + delta, count - 1))
        self.invalidate()

    def _move_attachment_selection(self, delta: int) -> None:
        count = self._attachment_selectable_count()
        if count <= 0:
            self._attachment_selected = 0
            return
        self._attachment_selected = max(0, min(self._attachment_selected + delta, count - 1))
        self.invalidate()

    def _move_skill_selection(self, delta: int) -> None:
        count = self._skill_selectable_count()
        if count <= 0:
            self._skill_selected = 0
            return
        self._skill_selected = max(0, min(self._skill_selected + delta, count - 1))
        self.invalidate()

    def _accept_command_panel_selection(self) -> bool:
        filtered = self._filtered_commands()
        if not filtered:
            return False
        selected = filtered[min(self._command_selected, len(filtered) - 1)][0]
        text = self._get_input_text().strip()
        if text == selected or text.startswith(selected + " "):
            return False
        self._input_lines = [selected]
        self._cursor_row = 0
        self._cursor_col = len(selected)
        self._command_panel_active = False
        self.invalidate()
        return True

    def _accept_skill_panel_selection(self) -> bool:
        token = self._skill_token()
        if token is None:
            return False
        matches = self._skill_matches()
        if not matches:
            return False
        selected = matches[min(self._skill_selected, len(matches) - 1)]
        replacement = f"${selected.name} "
        text = self._get_input_text()
        new_text = text[:token.start] + replacement + text[token.end:]
        new_cursor = token.start + len(replacement)
        self._set_input_text_and_cursor(new_text, new_cursor)
        self._skill_panel_suppressed_text = ""
        self._skill_selected = 0
        self.invalidate()
        return True

    def _accept_attachment_panel_selection(self) -> bool:
        token = self._attachment_token()
        if token is None:
            return False
        matches = self._attachment_matches()
        if not matches:
            return False
        selected = matches[min(self._attachment_selected, len(matches) - 1)]
        is_dir = selected.kind == "dir"
        if is_dir:
            replacement = "@" + selected.rel_path
        else:
            replacement = attachment_token_text(selected.rel_path) + " "
        text = self._get_input_text()
        new_text = text[:token.start] + replacement + text[token.end:]
        new_cursor = token.start + len(replacement)
        self._set_input_text_and_cursor(new_text, new_cursor)
        self._attachment_panel_suppressed_text = ""
        self._attachment_selected = 0
        if is_dir:
            self._attachment_matches_cache_key = None
        self.invalidate()
        return True

    # ── choice ───────────────────────────────────────────────────────────

    def _move_choice(self, delta: int) -> None:
        if self._active_choice is None:
            return
        n = len(self._active_choice)
        if n == 0:
            return
        selected = (self._choice_selected + delta) % n
        if selected == self._choice_selected:
            return
        self._choice_selected = selected
        self._choice_selection_render_pending = True

    def _finish_choice(self, value: str | None) -> None:
        self._choice_queue.put_nowait(value)

    def _submit_choice_selection(self) -> None:
        if self._active_choice is None:
            return
        _label, value, _desc = self._active_choice[self._choice_selected]
        self._finish_choice(value)

    @staticmethod
    def _normalize_choice_detail(item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            return item
        model_dump = getattr(item, "model_dump", None)
        if callable(model_dump):
            value = model_dump()
            return value if isinstance(value, dict) else {}
        return {}


def _skill_settings_signature(
    workspace: str,
) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    root = voidx_workspace_dir(workspace)
    return (
        _file_signature(root / "skills.json"),
        _file_signature(root / "settings.json"),
    )


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)
