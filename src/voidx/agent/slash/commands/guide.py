"""Slash /guide commands."""
from __future__ import annotations

from pathlib import Path
from voidx.agent.slash.init_prompt import INIT_PROMPT
from voidx.agent.domain.task.intent import InteractionMode


class GuideCommandsMixin:
    async def _guide(self, text: str) -> None:
        guidance = text.strip()
        if not guidance:
            self.host.ui.print("[dim]Usage: /guide <guidance for the next agent step>[/dim]")
            return
        if not self.host.can_submit_guidance():
            self.host.ui.print("[dim]Guidance is not available in this session.[/dim]")
            return
        if not self.host.submit_guidance(guidance):
            self.host.ui.print("[dim]No guidance submitted.[/dim]")

    async def _init(self, args: str) -> None:
        arg = args.strip().lower()
        if arg not in {"", "force"}:
            self.host.ui.error("Usage: /init [force]")
            return

        if self.host.interaction_mode_value() == InteractionMode.PLAN.value:
            self.host.ui.error("/init writes AGENTS.md. Run /unplan first.")
            return

        existing = Path(self.host.workspace) / "AGENTS.md"
        if existing.exists() and arg != "force":
            self.host.ui.print("[dim]AGENTS.md already exists. Use /init force to regenerate.[/dim]")
            return

        await self.host.run_coding_turn(INIT_PROMPT, display_text="/init")

