"""Mid-turn guidance slash command."""

from __future__ import annotations

from voidx.agent.slash.runtime import ui


class SlashGuideMixin:
    async def _guide(self, text: str) -> None:
        guidance = text.strip()
        if not guidance:
            ui.print("[dim]Usage: /guide <guidance for the next agent step>[/dim]")
            return
        submitter = getattr(self._g, "submit_guidance", None)
        if not callable(submitter):
            ui.print("[dim]Guidance is not available in this session.[/dim]")
            return
        if not submitter(guidance):
            ui.print("[dim]No guidance submitted.[/dim]")
