"""Slash command handler — routes slash commands to domain mixins."""
from __future__ import annotations

import asyncio
from inspect import isawaitable
from typing import Any
from voidx.presentation.slash.port import (
    AutomationSlashPort,
    IntegrationsSlashPort,
    ModeSlashPort,
    ModelSlashPort,
    PreferencesSlashPort,
    SessionSlashPort,
    SlashControlPort,
)
from voidx.presentation.slash.registry import REGISTRY, SLASH_COMMANDS, SlashCommand
from voidx.presentation.slash.runtime import prompt_text
from voidx.agent.ports.ui import NullAgentUiPort

ui = NullAgentUiPort().ui
from voidx.presentation.slash.commands import (
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
    CompactModelCommandsMixin,
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
    CompactModelCommandsMixin,
):
    def __init__(
        self,
        control_port: SlashControlPort,
        automation_port: AutomationSlashPort,
        mode_port: ModeSlashPort,
        session_port: SessionSlashPort,
        model_port: ModelSlashPort,
        integrations_port: IntegrationsSlashPort,
        preferences_port: PreferencesSlashPort,
        *,
        session_repository: Any | None = None,
        session_cleanup: Any | None = None,
    ) -> None:
        self.control_port = control_port
        self.automation_port = automation_port
        self.mode_port = mode_port
        self.session_port = session_port
        self.model_port = model_port
        self.integrations_port = integrations_port
        self.preferences_port = preferences_port
        self.session_repository = session_repository
        self.session_cleanup = session_cleanup

    async def _prompt(self, text: str, default: str = "", secret: bool = False) -> str | None:
        return await prompt_text(self.control_port.prompt_ui, text, default=default, secret=secret)

    def _noop(self) -> None:
        return None

    async def _cmd_plan(self) -> None:
        self._set_interaction_mode("plan")
        await self.control_port.persist_runtime_state()

    async def _cmd_unplan(self) -> None:
        self._set_interaction_mode("auto")
        await self.control_port.persist_runtime_state()

    def _cmd_allow(self, args: str) -> None:
        if args:
            self.control_port.permission_ops.allow(args)

    def _cmd_deny(self, args: str) -> None:
        if args:
            self.control_port.permission_ops.deny(args)

    async def _cmd_compact(self) -> None:
        compacted = await self.control_port.compact_session_history(force=True)
        if compacted:
            self.control_port.ui.print("[dim]Compacted context.[/dim]")
        else:
            self.control_port.ui.print("[dim]Nothing to compact.[/dim]")

    def _cmd_permissions(self) -> None:
        self.control_port.ui.print(self.control_port.permission_ops.show_rules())

    def _show_help(self) -> None:
        self.control_port.ui.print("[bold]Commands:[/bold]")
        for spec in SLASH_COMMANDS:
            name, desc = spec.name, spec.desc
            self.control_port.ui.print(f"  [cyan]{name}[/cyan] — {desc}")

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

