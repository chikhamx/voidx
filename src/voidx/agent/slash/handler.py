"""Slash command handler — routes slash commands to domain mixins."""
from __future__ import annotations

from inspect import isawaitable
from typing import Any
from voidx.agent.slash.runtime import prompt_text
from voidx.runtime.ui import COMMANDS, ui
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

    async def _prompt(self, text: str, default: str = "", secret: bool = False) -> str | None:
        return await prompt_text(self.host.app, text, default=default, secret=secret)

    async def dispatch(self, inp: str) -> bool:
        parts = inp.split(None, 1)
        cmd = parts[0]
        args = parts[1] if len(parts) > 1 else ""

        async def set_plan() -> None:
            self._set_interaction_mode("plan")
            await self.host.persist_runtime_state()

        async def set_auto() -> None:
            self._set_interaction_mode("auto")
            await self.host.persist_runtime_state()

        def allow_tool() -> None:
            tool = args or cmd.removeprefix("/allow").strip()
            if tool:
                self.host.permission.allow(tool)

        def deny_tool() -> None:
            tool = args or cmd.removeprefix("/deny").strip()
            if tool:
                self.host.permission.deny(tool)

        async def compact() -> None:
            compacted = await self.host.compact_session_history(force=True)
            if compacted:
                ui.print("[dim]Compacted context.[/dim]")
            else:
                ui.print("[dim]Nothing to compact.[/dim]")

        def show_help() -> None:
            ui.print("[bold]Commands:[/bold]")
            for name, desc in COMMANDS:
                ui.print(f"  [cyan]{name}[/cyan] — {desc}")

        handlers = {
            "/exit": lambda: None,
            "/quit": lambda: None,
            "/clear": self._clear,
            "/code-ide": lambda: self._code_ide(args),
            "/list": self._list_sessions,
            "/session": lambda: self._session(args),
            "/chat": lambda: self._chat_shortcut(args),
            "/resume": lambda: self._resume(inp),

            "/rollback": self._rollback,
            "/title": lambda: self._set_title(inp),
            "/mode": lambda: self._mode(args),
            "/goal": lambda: self._goal(args),
            "/guide": lambda: self._guide(args),
            "/init": lambda: self._init(args),
            "/lang": lambda: self._lang(args),
            "/plan": set_plan,
            "/unplan": set_auto,
            "/allow": allow_tool,
            "/deny": deny_tool,
            "/permissions": lambda: ui.print(self.host.permission.show_rules()),
            "/permission": lambda: self._permission_mode(args),
            "/usage": self._usage,
            "/upgrade": lambda: self._upgrade(args),
            "/mcp": lambda: self._mcp(args),
            "/lsp": lambda: self._lsp(args),
            "/loop": lambda: self._loop(args),
            "/skills": lambda: self._skills(args),
            "/paste": self._paste_clipboard_image,
            "/tone": lambda: self._tone(args),
            "/parallel": lambda: self._parallel(args),
            "/debug": lambda: self._debug(args),
            "/log": lambda: self._log(args),
            "/compact": compact,
            "/diff": self._show_diff,
            "/tavily": lambda: self._tavily(args),
            "/bocha": lambda: self._bocha(args),
            "/model": lambda: self._dispatch_model(args),
            "/help": show_help,
        }
        handler = handlers.get(cmd)
        if handler is None:
            return False

        result = handler()
        if isawaitable(result):
            await result
        return True

