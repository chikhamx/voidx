"""Slash command support for /mcp operations."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from collections.abc import Mapping

from voidx.agent.slash.runtime import _select_from_list, ui
from voidx.config import McpServerConfig, WebToolRoute


class SlashMcpMixin:
    async def _mcp(self, args: str) -> None:
        parts = args.split(None, 1)
        action = parts[0] if parts else ""
        target = parts[1].strip() if len(parts) > 1 else ""

        if action == "new":
            await self._mcp_new()
        elif action == "list":
            await self._mcp_list()
        elif action == "test" or action.startswith("test "):
            await self._mcp_test(target)
        elif action == "del" or action.startswith("del "):
            await self._mcp_del(target)
        elif action == "restart" or action.startswith("restart "):
            await self._mcp_restart(target)
        elif action == "tools" or action.startswith("tools "):
            await self._mcp_tools(target)
        elif action:
            ui.error("Usage: /mcp [new|list|test|del|restart|tools]")
        else:
            await self._mcp_list()

    async def _mcp_new(self) -> None:
        settings = self._g._settings
        if settings is None:
            ui.error("No settings file available.")
            return

        ui.print("[bold]Configure MCP server[/bold]")
        choices = ["voidx-web (built-in)", "Tavily MCP", "URL (SSE / Streamable HTTP)", "Custom command"]
        idx = await _select_from_list(self._g._app, "MCP server type", choices)
        if idx is None:
            ui.print("[dim]Cancelled.[/dim]")
            return

        web_routes: Mapping[str, WebToolRoute] = {}
        if choices[idx].startswith("voidx-web"):
            server = await self._mcp_builtin_web_config()
            if server is None:
                return
            web_routes = {
                "search": WebToolRoute(backend="mcp", server=server.name, tool="web_search"),
                "fetch": WebToolRoute(backend="mcp", server=server.name, tool="web_fetch"),
            }
        elif choices[idx].startswith("Tavily"):
            server = await self._mcp_tavily_config()
            if server is None:
                return
            web_routes = {
                "search": WebToolRoute(backend="mcp", server=server.name, tool="tavily_search"),
                "fetch": WebToolRoute(backend="mcp", server=server.name, tool="tavily_extract"),
            }
        elif choices[idx].startswith("URL"):
            server = await self._mcp_url_config()
            if server is None:
                return
        else:
            server = await self._mcp_custom_config()
            if server is None:
                return

        ok, tools, err = await self._test_mcp_config(server)
        if not ok:
            ui.error(f"MCP connection failed: {err}")
            ui.print("[dim]Configuration not saved. Check the command and try again.[/dim]")
            return

        tool_names = [tool.name for tool in tools]
        if tool_names:
            server.tools = tool_names

        path = settings.save_mcp_server(server)
        for kind, route in web_routes.items():
            settings.set_web_tool_route(kind, route)

        manager = getattr(self._g, "_mcp_manager", None)
        if manager is not None:
            try:
                await asyncio.wait_for(manager.restart_all(), timeout=30.0)
            except asyncio.TimeoutError:
                ui.warn("MCP restart timed out; servers may still be connecting in the background.")

        ui.print(
            f"  [cyan]{server.name}[/cyan] [green]✓ configured[/green]"
            f" · {len(tool_names)} tool{'s' if len(tool_names) != 1 else ''}"
        )
        if web_routes:
            ui.print("[dim]websearch/webfetch now use this MCP server[/dim]")
        ui.print(f"[dim]Saved to {path}[/dim]")

    async def _mcp_builtin_web_config(self) -> McpServerConfig | None:
        name = await self._prompt("Server name", default="voidx-web")
        if name is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        name = name.strip() or "voidx-web"
        env = {}
        tavily_key = self._g._settings.get_tavily_api_key() if self._g._settings else None
        if tavily_key:
            env["TAVILY_API_KEY"] = tavily_key
        return McpServerConfig(
            name=name,
            command=sys.executable,
            args=["-m", "voidx.mcp_servers.web"],
            env=env,
            tools=["web_search", "web_fetch"],
        )

    async def _mcp_tavily_config(self) -> McpServerConfig | None:
        name = await self._prompt("Server name", default="tavily")
        if name is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        name = name.strip() or "tavily"

        env = {}
        env_key = os.environ.get("TAVILY_API_KEY")
        tavily_key = self._g._settings.get_tavily_api_key() if self._g._settings else None
        if not env_key and tavily_key:
            env["TAVILY_API_KEY"] = tavily_key
        elif not env_key:
            tavily_key = await self._prompt("Tavily API key", secret=True)
            if tavily_key is None:
                ui.print("[dim]Cancelled.[/dim]")
                return None
            tavily_key = tavily_key.strip()
            if not tavily_key:
                ui.error("Tavily API key is required.")
                return None
            env["TAVILY_API_KEY"] = tavily_key

        return McpServerConfig(
            name=name,
            command="npx",
            args=["-y", "tavily-mcp@latest"],
            env=env,
            tools=["tavily_search", "tavily_extract"],
        )

    async def _mcp_custom_config(self) -> McpServerConfig | None:
        name = await self._prompt("Server name")
        if name is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        name = name.strip()
        if not name:
            ui.error("Server name is required.")
            return None

        command = await self._prompt("Command")
        if command is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        command = command.strip()
        if not command:
            ui.error("Command is required.")
            return None

        args_text = await self._prompt("Args (shell-style, optional)", default="")
        if args_text is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        try:
            args = shlex.split(args_text)
        except ValueError as exc:
            ui.error(f"Invalid args: {exc}")
            return None

        env_text = await self._prompt("Env VAR=value,VAR2=value2 (optional)", default="")
        if env_text is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        env = _parse_env_pairs(env_text)
        return McpServerConfig(name=name, command=command, args=args, env=env)

    async def _mcp_url_config(self) -> McpServerConfig | None:
        name = await self._prompt("Server name")
        if name is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        name = name.strip()
        if not name:
            ui.error("Server name is required.")
            return None

        transport_choices = ["SSE (legacy)", "Streamable HTTP (MCP 2024-11-05)"]
        t_idx = await _select_from_list(self._g._app, "Transport type", transport_choices)
        if t_idx is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        transport = "sse" if t_idx == 0 else "streamable-http"

        url_hint = "https://mcp.example.com/sse" if transport == "sse" else "http://127.0.0.1:52222/mcp/"
        url = await self._prompt(f"URL (e.g. {url_hint})")
        if url is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        url = url.strip()
        if not url:
            ui.error("URL is required.")
            return None
        if not url.startswith(("http://", "https://")):
            ui.error("URL must start with http:// or https://")
            return None

        env_text = await self._prompt("Env VAR=value,VAR2=value2 (optional)", default="")
        if env_text is None:
            ui.print("[dim]Cancelled.[/dim]")
            return None
        env = _parse_env_pairs(env_text)
        return McpServerConfig(name=name, url=url, transport=transport, env=env)

    async def _mcp_list(self) -> None:
        settings = self._g._settings
        if settings is None:
            ui.print("[dim]No settings file available.[/dim]")
            return

        manager = getattr(self._g, "_mcp_manager", None)
        statuses = manager.statuses() if manager is not None and manager.started else []
        ui.print("[bold]MCP servers:[/bold]")
        ui.print(f"[dim]{settings.path}[/dim]")
        if statuses:
            for status in statuses:
                self._print_mcp_status(status)
        else:
            servers = settings.list_mcp_servers()
            if not servers:
                ui.print("[dim]No MCP servers configured. Use /mcp new.[/dim]")
                return
            for server in servers:
                state = "[dim]disabled[/dim]" if server.disabled else "[green]configured[/green]"
                tools = f"{server.tool_count} tool{'s' if server.tool_count != 1 else ''}"
                ui.print(f"  [cyan]{server.name}[/cyan] · {state} · [dim]{tools}[/dim]")

        search = settings.get_web_tool_route("search")
        fetch = settings.get_web_tool_route("fetch")
        if search.backend == "mcp" or fetch.backend == "mcp":
            ui.print()
            ui.print("[bold]Web routing:[/bold]")
            ui.print(f"  search · {search.backend} {search.server}/{search.tool}".rstrip("/"))
            ui.print(f"  fetch  · {fetch.backend} {fetch.server}/{fetch.tool}".rstrip("/"))
        ui.print("[dim]Usage: /mcp new|list|test|del|restart|tools[/dim]")

    async def _mcp_test(self, target: str) -> None:
        async def _do_test(name: str) -> None:
            server = self._g._settings.get_mcp_server(name) if self._g._settings else None
            if server is None:
                ui.error(f"MCP server not found: {name}")
                return
            ok, tools, err = await self._test_mcp_config(server)
            if ok:
                names = ", ".join(tool.name for tool in tools) or "no tools"
                ui.print(f"[green]✓ {name} — connected[/green] [dim]{names}[/dim]")
            else:
                ui.print(f"[red]✗ {name} — {err}[/red]")

        await self._pick_mcp_server("Test", target, _do_test)

    async def _mcp_del(self, target: str) -> None:
        async def _do_delete(name: str) -> None:
            if self._g._settings is None:
                ui.error("No settings file available.")
                return
            path = self._g._settings.delete_mcp_server(name)
            manager = getattr(self._g, "_mcp_manager", None)
            if manager is not None:
                try:
                    await asyncio.wait_for(manager.restart_all(), timeout=30.0)
                except asyncio.TimeoutError:
                    ui.warn("MCP restart timed out after deletion; servers may still be reconnecting.")
            ui.print(f"[dim]'{name}' removed.[/dim]")
            ui.print(f"[dim]Cleaned {path}[/dim]")

        await self._pick_mcp_server("Delete", target, _do_delete)

    async def _mcp_restart(self, target: str) -> None:
        _ = target
        manager = getattr(self._g, "_mcp_manager", None)
        if manager is None:
            ui.error("No MCP manager available.")
            return
        try:
            await asyncio.wait_for(manager.restart_all(), timeout=30.0)
        except asyncio.TimeoutError:
            ui.warn("MCP restart timed out; servers may still be connecting in the background.")
            return
        ui.print("[green]✓ MCP servers restarted[/green]")

    async def _mcp_tools(self, target: str) -> None:
        async def _do_tools(name: str) -> None:
            manager = getattr(self._g, "_mcp_manager", None)
            if manager is None:
                ui.error("No MCP manager available.")
                return
            try:
                tools = await asyncio.wait_for(
                    manager.list_tools_for_server(name), timeout=15.0,
                )
            except asyncio.TimeoutError:
                ui.error(f"Listing tools for {name} timed out.")
                return
            except Exception as exc:
                ui.error(f"Could not list tools for {name}: {exc}")
                return
            ui.print(f"[bold]{name} tools:[/bold]")
            if not tools:
                ui.print("[dim]No tools.[/dim]")
                return
            for tool in tools:
                ui.print(f"  [cyan]{tool.name}[/cyan] — {tool.description or '(no description)'}")

        await self._pick_mcp_server("Tools", target, _do_tools)

    async def _pick_mcp_server(self, action: str, target: str, callback) -> None:
        if self._g._settings is None:
            ui.error("No settings file available.")
            return
        if target:
            await callback(target)
            return
        names = [server.name for server in self._g._settings.list_mcp_servers()]
        if not names:
            ui.print("[yellow]No MCP servers configured. Use /mcp new first.[/yellow]")
            return
        ui.print(f"[bold]{action}[/bold] — select MCP server:")
        idx = await _select_from_list(self._g._app, action, names)
        if idx is None:
            ui.print("[dim]Cancelled.[/dim]")
            return
        await callback(names[idx])

    @staticmethod
    async def _test_mcp_config(server: McpServerConfig, timeout: float = 30.0):
        from voidx.mcp.client import McpClient

        client = McpClient(server)
        try:
            await asyncio.wait_for(client.start(), timeout=timeout)
            tools = await asyncio.wait_for(client.list_tools(), timeout=timeout)
            return True, tools, ""
        except asyncio.TimeoutError:
            return False, [], f"connection timed out after {timeout:.0f}s"
        except Exception as exc:
            return False, [], str(exc)
        finally:
            try:
                await asyncio.wait_for(client.stop(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass

    @staticmethod
    def _print_mcp_status(status) -> None:
        if status.status == "connected":
            state = "[green]connected[/green]"
        elif status.status == "connecting":
            state = "[yellow]connecting…[/yellow]"
        elif status.status == "error":
            state = "[red]error[/red]"
        elif status.status == "disabled":
            state = "[dim]disabled[/dim]"
        else:
            state = "[yellow]disconnected[/yellow]"
        tools = f"{status.tool_count} tool{'s' if status.tool_count != 1 else ''}" if status.tool_count else ""
        err = f" · [dim]{status.error_message}[/dim]" if status.error_message else ""
        ui.print(f"  [cyan]{status.name}[/cyan] · {state}{f' · {tools}' if tools else ''}{err}")


def _parse_env_pairs(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in text.split(","):
        item = raw.strip()
        if not item:
            continue
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if key:
            result[key] = value.strip()
    return result
