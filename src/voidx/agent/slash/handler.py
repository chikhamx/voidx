"""Slash command handler — routes slash commands to domain mixins."""
from __future__ import annotations

import asyncio
from inspect import isawaitable
from typing import Any
from voidx.agent.slash.registry import REGISTRY, SLASH_COMMANDS, SlashCommand
from voidx.agent.slash.runtime import prompt_text
from voidx.agent.ports.ui import NullAgentUiPort

ui = NullAgentUiPort().ui
from voidx.agent.slash.commands import (
    ModeCommandsMixin,
    SessionCommandsMixin,
    ModelCommandsMixin,
    ProfileCommandsMixin,
    PermissionCommandsMixin,
    WebCommandsMixin,
    IdeCommandsMixin,
    GuideCommandsMixin,
    LspCommandsMixin,
    SkillsCommandsMixin,
    UpgradeCommandsMixin,
    McpCommandsMixin,
    LoopCmdCommandsMixin,
)


class SlashHandler(
    ModeCommandsMixin,
    SessionCommandsMixin,
    ModelCommandsMixin,
    ProfileCommandsMixin,
    PermissionCommandsMixin,
    WebCommandsMixin,
    IdeCommandsMixin,
    GuideCommandsMixin,
    LspCommandsMixin,
    SkillsCommandsMixin,
    UpgradeCommandsMixin,
    McpCommandsMixin,
    LoopCmdCommandsMixin,
):
    def __init__(self, commands: Any) -> None:
        self.host = commands
        if not hasattr(commands, "ui"):
            commands.ui = ui
        if not hasattr(commands, "_ui"):
            commands._ui = NullAgentUiPort()

    async def _prompt(self, text: str, default: str = "", secret: bool = False) -> str | None:
        return await prompt_text(self.host.app, text, default=default, secret=secret)

    def _noop(self) -> None:
        return None

    async def _cmd_plan(self) -> None:
        self._set_interaction_mode("plan")
        await self.host.persist_runtime_state()

    async def _cmd_unplan(self) -> None:
        self._set_interaction_mode("auto")
        await self.host.persist_runtime_state()

    def _cmd_allow(self, args: str) -> None:
        if args:
            self.host.permission.allow(args)

    def _cmd_deny(self, args: str) -> None:
        if args:
            self.host.permission.deny(args)

    async def _cmd_compact(self) -> None:
        compacted = await self.host.compact_session_history(force=True)
        if compacted:
            self.host.ui.print("[dim]Compacted context.[/dim]")
        else:
            self.host.ui.print("[dim]Nothing to compact.[/dim]")

    def _cmd_permissions(self) -> None:
        self.host.ui.print(self.host.permission.show_rules())

    def _show_help(self) -> None:
        self.host.ui.print("[bold]Commands:[/bold]")
        for spec in SLASH_COMMANDS:
            name, desc = spec.name, spec.desc
            self.host.ui.print(f"  [cyan]{name}[/cyan] — {desc}")

    def _handler_for(self, spec: SlashCommand, args: str, inp: str):
        method = getattr(self, spec.method)
        if spec.arg == "none":
            return method
        if spec.arg == "inp":
            return lambda: method(inp)
        return lambda: method(args)

    async def dispatch(self, inp: str) -> bool:
        parts = inp.split(None, 1)
        cmd = parts[0]
        args = parts[1] if len(parts) > 1 else ""

        spec = REGISTRY.get(cmd)
        if spec is None:
            return False

        result = self._handler_for(spec, args, inp)()
        if isawaitable(result):
            await result
        return True

