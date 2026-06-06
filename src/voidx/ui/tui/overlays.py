"""Choice, attachment, and command overlay rendering."""

from __future__ import annotations

from voidx.ui.tui.helpers import _candidate_meta, _escape_markup


class _OverlayRendererMixin:
    def _render_choice_overlay(self, width: int) -> list[str]:
        if self._active_choice is None:
            return []
        result: list[str] = []
        result.append(f"[bold yellow]?[/bold yellow] {_escape_markup(self._choice_prompt)}")
        total = len(self._active_choice)
        max_visible = 10
        if total <= max_visible:
            start = 0
            visible = self._active_choice
        else:
            start = max(0, min(self._choice_selected - max_visible + 1, total - max_visible))
            visible = self._active_choice[start:start + max_visible]
        if start > 0:
            result.append(f"  [dim]... {start} above[/dim]")
        for offset, (label, value, desc) in enumerate(visible):
            i = start + offset
            marker = "❯" if i == self._choice_selected else " "
            label_text = _escape_markup(label)
            desc_text = _escape_markup(desc)
            if i == self._choice_selected:
                label_text = f"[bold blue]{label_text}[/bold blue]"
            line = f"  {marker} {label_text}  {desc_text}"
            # Truncate to fit terminal width (Rich markup makes exact measurement
            # expensive; use a generous safety margin)
            max_len = max(width * 2, 80)
            if len(line) > max_len:
                line = line[:max_len - 1] + "…"
            result.append(line)
        remaining = total - start - len(visible)
        if remaining > 0:
            result.append(f"  [dim]... {remaining} below[/dim]")
        for detail in self._choice_details[:8]:
            name = _escape_markup(str(detail.get("name", "")))
            pattern = _escape_markup(str(detail.get("pattern", "")))
            if pattern:
                result.append(f"    [dim]{name}: {pattern}[/dim]")
            elif name:
                result.append(f"    [dim]{name}[/dim]")
        return result

    def _render_panel_lines(self, width: int) -> list[str]:
        lines: list[str] = []
        lines.extend(self._render_command_palette(width))
        lines.extend(self._render_attachment_panel(width))
        lines.extend(self._render_choice_overlay(width))
        return lines

    def _render_attachment_panel(self, width: int) -> list[str]:
        if not self._attachment_panel_active():
            return []
        matches = self._attachment_matches()
        token = self._attachment_token()
        query = token.query if token is not None else ""
        detail = f"{len(matches)} match{'es' if len(matches) != 1 else ''}"
        if query:
            detail += f" for @{_escape_markup(query)}"
        result = [
            "[bold]Attach files[/bold]",
            f"[dim]{detail}[/dim]",
        ]
        if not matches:
            result.append("    [dim]No matching files[/dim]")
            return result
        selected = min(self._attachment_selected, len(matches) - 1)
        visible_count = min(len(matches), 8)
        start = max(0, min(selected - visible_count + 1, len(matches) - visible_count))
        visible = matches[start:start + visible_count]
        if start > 0:
            result.append(f"  [dim]... {start} above[/dim]")
        for offset, candidate in enumerate(visible):
            index = start + offset
            marker = "❯" if index == selected else " "
            style = "bold cyan" if index == selected else "dim"
            meta = _candidate_meta(candidate)
            result.append(
                f"  {marker} [{style}]{_escape_markup(candidate.rel_path)}[/{style}]"
                f"[dim]{_escape_markup(meta)}[/dim]"
            )
        remaining = len(matches) - start - len(visible)
        if remaining > 0:
            result.append(f"  [dim]... {remaining} below[/dim]")
        return result

    def _render_command_palette(self, width: int) -> list[str]:
        if not self._command_panel_active:
            return []
        filtered = self._filtered_commands()
        if not filtered:
            return []
        max_items = min(len(filtered), 8)
        result: list[str] = []
        for i in range(max_items):
            name, desc = filtered[i]
            marker = "❯" if i == self._command_selected else " "
            name_style = "bold cyan" if i == self._command_selected else "dim"
            desc_style = "white" if i == self._command_selected else "dim"
            result.append(
                f"  {marker} [{name_style}]{name}[/{name_style}]"
                f"  [{desc_style}]{_escape_markup(desc)}[/{desc_style}]"
            )
        if len(filtered) > max_items:
            result.append(f"  [dim]… and {len(filtered) - max_items} more[/dim]")
        return result

