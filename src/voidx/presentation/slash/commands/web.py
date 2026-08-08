"""Slash /web commands."""
from __future__ import annotations

import asyncio
from voidx.mcp.domain.config import McpServerConfig
from voidx.tooling.domain.web import WebToolRoute


class WebCommandsMixin:
    async def _tavily(self, args: str) -> None:
        """Configure Tavily API key for web search."""
        settings = self.integrations_port.integration_settings
        if not settings:
            self.integrations_port.ui.error("No settings available.")
            return

        if not args or args.strip() == "show":
            key = settings.get_tavily_api_key()
            if key:
                self.integrations_port.ui.print(f"Tavily API key: [cyan]{self._mask_key(key)}[/cyan]")
            else:
                self.integrations_port.ui.print("[dim]Tavily API key not configured. Using DuckDuckGo fallback.[/dim]")
            self.integrations_port.ui.print("[dim]Usage: /tavily set | /tavily delete[/dim]")
            return

        parts = args.split(None, 1)
        action = parts[0].strip().lower() if parts else ""
        if action == "set":
            if len(parts) > 1 and parts[1].strip():
                self.integrations_port.ui.error("Do not include the API key in command text. Use /tavily set.")
                return
            api_key = await self._prompt("Tavily API key", default="", secret=True)
            if api_key is None:
                self.integrations_port.ui.print("[dim]Cancelled.[/dim]")
                return
            api_key = api_key.strip()
            if not api_key:
                self.integrations_port.ui.error("Tavily API key is required.")
                return
            settings.set_tavily_api_key(api_key)
            await self._sync_tavily_mcp_config(api_key)
            self.integrations_port.ui.print(f"Tavily API key saved: [cyan]{self._mask_key(api_key)}[/cyan]")
            self.integrations_port.ui.print("[dim]Tavily MCP server configured for websearch/webfetch.[/dim]")
        elif args.strip() == "delete":
            settings.delete_tavily_api_key()
            await self._remove_tavily_mcp_key()
            self.integrations_port.ui.print("[dim]Tavily API key deleted. Using DuckDuckGo fallback.[/dim]")
        else:
            self.integrations_port.ui.print("[dim]Usage: /tavily [set|delete|show][/dim]")

    async def _bocha(self, args: str) -> None:
        """Configure Bocha API key for web search."""
        settings = self.integrations_port.integration_settings
        if not settings:
            self.integrations_port.ui.error("No settings available.")
            return
        if not args or args.strip() == "show":
            key = settings.get_bocha_api_key()
            if key:
                self.integrations_port.ui.print(f"Bocha API key: [cyan]{self._mask_key(key)}[/cyan]")
            else:
                self.integrations_port.ui.print("[dim]Bocha API key not configured. Using crawler fallbacks.[/dim]")
            self.integrations_port.ui.print("[dim]Usage: /bocha set | /bocha delete[/dim]")
            return
        action = args.split(None, 1)[0].strip().lower()
        if action == "set":
            api_key = await self._prompt("Bocha API key", default="", secret=True)
            if api_key is None:
                self.integrations_port.ui.print("[dim]Cancelled.[/dim]")
                return
            api_key = api_key.strip()
            if not api_key:
                self.integrations_port.ui.error("Bocha API key is required.")
                return
            settings.set_bocha_api_key(api_key)
            self.integrations_port.ui.print(f"Bocha API key saved: [cyan]{self._mask_key(api_key)}[/cyan]")
        elif args.strip() == "delete":
            settings.delete_bocha_api_key()
            self.integrations_port.ui.print("[dim]Bocha API key deleted. Using crawler fallbacks.[/dim]")
        else:
            self.integrations_port.ui.print("[dim]Usage: /bocha [set|delete|show][/dim]")

    async def _sync_tavily_mcp_config(self, api_key: str) -> None:

        settings = self.integrations_port.integration_settings
        if settings is None:
            return
        existing = settings.get_mcp_server("tavily")
        if existing is None:
            server = McpServerConfig(
                name="tavily",
                command="npx",
                args=["-y", "tavily-mcp@latest"],
                env={"TAVILY_API_KEY": api_key},
                tools=["tavily_search", "tavily_extract"],
            )
        else:
            server = existing.model_copy(
                update={"env": {**existing.env, "TAVILY_API_KEY": api_key}},
            )
        settings.save_mcp_server(server)
        settings.set_web_tool_route(
            "search",
            WebToolRoute(backend="mcp", server="tavily", tool="tavily_search"),
        )
        settings.set_web_tool_route(
            "fetch",
            WebToolRoute(backend="mcp", server="tavily", tool="tavily_extract"),
        )
        await self._restart_mcp_manager_if_available()

    async def _remove_tavily_mcp_key(self) -> None:
        settings = self.integrations_port.integration_settings
        if settings is None:
            return
        existing = settings.get_mcp_server("tavily")
        if existing is not None and "TAVILY_API_KEY" in existing.env:
            env = dict(existing.env)
            env.pop("TAVILY_API_KEY", None)
            settings.save_mcp_server(existing.model_copy(update={"env": env}))
        settings.clear_web_routes_for_server("tavily", save=True)
        await self._restart_mcp_manager_if_available()

    async def _restart_mcp_manager_if_available(self) -> None:
        manager = self.integrations_port.mcp_ops
        if manager is None:
            return
        try:
            await asyncio.wait_for(manager.restart_all(), timeout=30.0)
        except asyncio.TimeoutError:
            self.integrations_port.ui.warn("MCP restart timed out; servers may still be connecting in the background.")

