"""Slash /guide commands."""
from __future__ import annotations

from pathlib import Path
from voidx.presentation.slash.init_prompt import INIT_PROMPT
from voidx.agent.domain.task.intent import InteractionMode


class GuideCommandsMixin:
    async def _guide(self, text: str) -> None:
        guidance = text.strip()
        if not guidance:
            self.automation_port.ui.print("[dim]Usage: /guide <guidance for the next agent step>[/dim]")
            return
        if not self.automation_port.can_submit_guidance():
            self.automation_port.ui.print("[dim]Guidance is not available in this session.[/dim]")
            return
        if not self.automation_port.submit_guidance(guidance):
            self.automation_port.ui.print("[dim]No guidance submitted.[/dim]")

    async def _init(self, args: str) -> None:
        arg = args.strip().lower()
        if arg not in {"", "force"}:
            self.automation_port.ui.error("Usage: /init [force]")
            return

        if self.automation_port.interaction_mode_value() == InteractionMode.PLAN.value:
            self.automation_port.ui.error("/init writes AGENTS.md. Run /unplan first.")
            return

        existing = Path(self.automation_port.workspace) / "AGENTS.md"
        if existing.exists() and arg != "force":
            self.automation_port.ui.print("[dim]AGENTS.md already exists. Use /init force to regenerate.[/dim]")
            return

        await self.automation_port.run_coding_turn(INIT_PROMPT, display_text="/init")

