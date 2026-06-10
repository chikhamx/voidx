"""Mid-turn guidance slash command."""

from __future__ import annotations

from voidx.runtime.ui import ui


class SlashGuideMixin:
    async def _guide(self, text: str) -> None:
        guidance = text.strip()
        if not guidance:
            ui.print("[dim]Usage: /guide <guidance for the next agent step>[/dim]")
            return
        if not self.host.can_submit_guidance():
            ui.print("[dim]Guidance is not available in this session.[/dim]")
            return
        if not self.host.submit_guidance(guidance):
            ui.print("[dim]No guidance submitted.[/dim]")
