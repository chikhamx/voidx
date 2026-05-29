"""Slash command handler — extracted from graph.py to keep it focused."""

from __future__ import annotations

from typing import TYPE_CHECKING

from voidx.agent.slash_parts.model import SlashModelMixin
from voidx.agent.slash_parts.runtime import PROVIDERS, _select_from_list, _w, ui

if TYPE_CHECKING:
    from voidx.agent.graph import VoidXGraph


class SlashHandler(SlashModelMixin):
    """Handles all slash commands (/help, /model, /plan, etc.).

    Takes a reference to the parent VoidXGraph since commands need access
    to session, config, permission, and model state.
    """

    def __init__(self, graph: VoidXGraph) -> None:
        self._g = graph

    async def dispatch(self, inp: str) -> bool:
        from voidx.ui.commands import COMMANDS

        parts = inp.split(None, 1)
        cmd = parts[0]
        args = parts[1] if len(parts) > 1 else ""

        known = [n for n, _ in COMMANDS if n == cmd]
        if not known:
            return False

        ui.print()

        if cmd in ("/exit", "/quit"):
            return True

        if cmd == "/clear":
            await self._clear()
        elif cmd == "/list":
            await self._list_sessions()
        elif cmd.startswith("/resume"):
            await self._resume(inp)
        elif cmd.startswith("/title"):
            await self._set_title(inp)
        elif cmd == "/plan":
            self._g._plan_mode = True
            ui.print("[yellow]PLAN MODE active. /unplan to exit.[/yellow]")
        elif cmd == "/unplan":
            self._g._plan_mode = False
            ui.print("[dim]Plan mode exited.[/dim]")
        elif cmd.startswith("/allow"):
            tool = args or cmd.removeprefix("/allow").strip()
            if tool:
                self._g._permission.allow(tool)
        elif cmd.startswith("/deny"):
            tool = args or cmd.removeprefix("/deny").strip()
            if tool:
                self._g._permission.deny(tool)
        elif cmd == "/permissions":
            ui.print(self._g._permission.show_rules())
        elif cmd == "/mcp":
            await self._mcp(args)
        elif cmd == "/paste":
            self._paste_clipboard_image()
        elif cmd.startswith("/debug"):
            self._debug(args)
        elif cmd == "/compact":
            ui.print("[yellow]Compacting...[/yellow]")
        elif cmd == "/diff":
            await self._show_diff()
        elif cmd == "/tavily":
            await self._tavily(args)
        elif cmd == "/model":
            if args == "config":
                await self._model_config()
            elif args == "list":
                await self._model_list()
            elif args == "test" or args.startswith("test "):
                target = args.removeprefix("test").strip()
                await self._model_test(target)
            elif args == "delete" or args.startswith("delete "):
                target = args.removeprefix("delete").strip()
                await self._model_delete(target)
            elif args == "switch" or args.startswith("switch "):
                target = args.removeprefix("switch").strip()
                await self._model_switch(target)
            elif args:
                await self._switch_model(args)
            else:
                await self._list_models()
        elif cmd == "/help":
            ui.print("[bold]Commands:[/bold]")
            for name, desc in COMMANDS:
                ui.print(f"  [cyan]{name}[/cyan] — {desc}")
        return True

    def _debug(self, arg: str) -> None:
        value = arg.strip().lower()
        if value in ("on", "true", "1", "yes"):
            self._g.set_debug(True)
        elif value in ("off", "false", "0", "no"):
            self._g.set_debug(False)
        elif value:
            ui.error("Usage: /debug [on|off]")
            return
        else:
            self._g.set_debug(not self._g._debug)

        state = "on" if self._g._debug else "off"
        ui.print(f"[dim]debug {state}[/dim]")

    def _paste_clipboard_image(self) -> None:
        app = getattr(self._g, "_app", None)
        if app is None or not hasattr(app, "paste_clipboard_image"):
            ui.error("/paste requires the interactive UI.")
            return
        result = app.paste_clipboard_image()
        if result.ok:
            ui.print(f"[dim]{result.message}[/dim]")
            return
        ui.error(result.message)

    async def _show_diff(self) -> None:
        from voidx.ui.diff import git_diff, git_diff_stat
        stat = git_diff_stat(self._g._workspace)
        if stat:
            ui.print(f"[bold]Changes:[/bold]\n{stat}\n")
            diff_text = git_diff(self._g._workspace)
            if diff_text:
                ui.diff(diff_text)
            else:
                ui.print("[dim]No diff content.[/dim]")
        else:
            ui.print("[dim]No changes in working tree.[/dim]")

    async def _mcp(self, args: str) -> None:
        settings = self._g._settings
        if settings is None:
            ui.print("[dim]No settings file available.[/dim]")
            return

        servers = settings.list_mcp_servers()
        ui.print("[bold]MCP servers:[/bold]")
        ui.print(f"[dim]{settings.path}[/dim]")
        if not servers:
            ui.print("[dim]No MCP servers configured. Add mcpServers to voidx.json.[/dim]")
            return

        for server in servers:
            state = "[dim]disabled[/dim]" if server.disabled else "[green]configured[/green]"
            tools = f"{server.tool_count} tool{'s' if server.tool_count != 1 else ''}"
            ui.print(f"  [cyan]{server.name}[/cyan] · {state} · [dim]{tools}[/dim]")

    async def _clear(self) -> None:
        if self._g._session:
            from voidx.memory.session import clear_messages, update_title
            await clear_messages(self._g._session.id)
            await update_title(self._g._session.id, "New session")
            self._g._tracker._todos = []
            self._g._permission.clear_session_permissions()
            self._g._plan_mode = False
        ui.print("[dim]✓ Session cleared — ready for a new conversation[/dim]")

    async def _list_sessions(self) -> None:
        from voidx.memory.session import list_sessions
        sessions = await list_sessions()
        if not sessions:
            ui.print("No saved sessions.")
            return

        ui.print("[bold]Sessions:[/bold]")
        items = []
        for s in sessions:
            title = s.title[:50] + ("..." if len(s.title) > 50 else "")
            items.append(f"{s.id[:8]} | {title} | {getattr(s, 'updated_at', '')[:16]}")
        
        idx = None
        if getattr(self._g, "_app", None):
            idx = await _select_from_list(self._g._app, "Resume session?", items)
        
        if idx is not None:
            await self._resume(f"/resume {sessions[idx].id}")

    async def _resume(self, cmd: str) -> None:
        from voidx.memory.session import get_session
        sid = cmd.removeprefix("/resume").strip()
        if not sid:
            ui.error("Usage: /resume <session_id>")
            return
        session = await get_session(sid)
        if not session:
            ui.error(f"Session not found: {sid}")
            return
        self._g._session = session
        self._g._workspace = session.workspace
        self._g.config.workspace = session.workspace
        ui.print(f"[dim]Resumed: {session.id} — {session.title} ({session.message_count} msgs)[/dim]")

    async def _set_title(self, cmd: str) -> None:
        from voidx.memory.session import update_title
        if not self._g._session:
            return
        title = cmd.removeprefix("/title").strip()
        if title:
            await update_title(self._g._session.id, title)
            ui.print(f"[dim]Title set: {title}[/dim]")

    async def _tavily(self, args: str) -> None:
        """Configure Tavily API key for web search."""
        settings = self._g._settings
        if not settings:
            ui.error("No settings available.")
            return

        if not args or args.strip() == "show":
            key = settings.get_tavily_api_key()
            if key:
                masked = key[:4] + "****" + key[-4:] if len(key) > 8 else key
                ui.print(f"Tavily API key: [cyan]{masked}[/cyan]")
            else:
                ui.print("[dim]Tavily API key not configured. Using DuckDuckGo fallback.[/dim]")
            ui.print("[dim]Usage: /tavily set <api_key> | /tavily delete[/dim]")
            return

        if args.startswith("set "):
            api_key = args[4:].strip()
            if api_key:
                settings.set_tavily_api_key(api_key)
                masked = api_key[:4] + "****" + api_key[-4:] if len(api_key) > 8 else api_key
                ui.print(f"Tavily API key saved: [cyan]{masked}[/cyan]")
            else:
                ui.error("Usage: /tavily set <api_key>")
        elif args.strip() == "delete":
            settings.delete_tavily_api_key()
            ui.print("[dim]Tavily API key deleted. Using DuckDuckGo fallback.[/dim]")
        else:
            ui.print("[dim]Usage: /tavily [set <api_key>|delete|show][/dim]")
