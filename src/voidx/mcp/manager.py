"""MCP manager — lifecycle orchestrator for all MCP server connections.

Responsibilities:
  - Reads server configs from Settings
  - Spawns McpClient per server
  - Discovers tools and registers them in ToolRegistry
  - Exposes status for UI
  - Clean shutdown on graph exit

Design: all servers start in parallel, a single failure doesn't block others.
Startup is non-blocking — start_all() returns immediately and servers connect
in the background. UI can poll statuses() to show progress.
"""

from __future__ import annotations

import asyncio
import logging

from voidx.config import McpServerConfig, Settings
from voidx.mcp.client import McpClient, McpConnectionError
from voidx.mcp.schema import McpCallResult, McpRuntimeStatus, McpToolDef
from voidx.mcp.tool import McpToolWrapper, mcp_tool_id
from voidx.permission.service import PermissionService
from voidx.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


class McpManager:
    """Manages all MCP server connections for a session."""

    def __init__(self, settings: Settings | None, registry: ToolRegistry, permission: PermissionService) -> None:
        self._settings = settings
        self._registry = registry
        self._permission = permission
        self._clients: dict[str, McpClient] = {}
        self._started = False
        self._tool_counts: dict[str, int] = {}
        self._errors: dict[str, str] = {}
        self._connecting: set[str] = set()
        self._init_task: asyncio.Task | None = None
        self._server_configs: list[McpServerConfig] = []

    @property
    def started(self) -> bool:
        return self._started

    # ── lifecycle ───────────────────────────────────────────────────────

    async def start_all(self) -> None:
        """Start all configured MCP servers in the background.

        Returns immediately. Servers connect asynchronously; poll statuses()
        or await wait_ready() to know when they're done.
        """
        if self._started:
            return
        self._started = True
        self._registry.unregister_prefix("mcp__")
        self._tool_counts.clear()

        if self._settings is None:
            return

        servers = self._settings.list_mcp_servers()
        if not servers:
            return

        self._server_configs = servers
        enabled = [s for s in servers if not s.disabled]
        if not enabled:
            return

        log.info("Starting %d MCP server(s) in background...", len(enabled))

        # Mark all as connecting
        for s in enabled:
            self._connecting.add(s.name)

        # Fire-and-forget: connect in background
        self._init_task = asyncio.create_task(self._init_servers(enabled))

    async def wait_ready(self, timeout: float = 30.0) -> None:
        """Wait until all background server connections are done (or timed out)."""
        if self._init_task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._init_task), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("MCP server init did not complete within %.0fs", timeout)

    async def _init_servers(self, servers: list[McpServerConfig]) -> None:
        """Connect to servers and register tools (runs in background)."""
        try:
            results = await self._start_servers(servers)

            # Register tools from successfully started servers
            for server_name, client in results:
                try:
                    tool_defs = await client.list_tools()
                except Exception as e:
                    log.warning("Could not list tools from MCP server '%s': %s", server_name, e)
                    self._errors[server_name] = f"Could not list tools: {e}"
                    self._tool_counts[server_name] = 0
                    continue
                self._errors.pop(server_name, None)

                allowed = self._resolve_tool_filter(
                    next((s for s in servers if s.name == server_name), None)
                )

                registered = 0
                for td in tool_defs:
                    if allowed is not None and td.name not in allowed:
                        continue
                    wrapper = McpToolWrapper(client, td, server_name)
                    self._registry.register(
                        wrapper.id,
                        wrapper,
                        wrapper.description,
                        wrapper.parameters_schema(),
                    )
                    registered += 1

                # Pre-deny tools that are in deny filter (user explicitly wants them blocked)
                disallowed = self._resolve_tool_filter(
                    next((s for s in servers if s.name == server_name), None),
                    allow_mode=False,
                )
                if disallowed:
                    for tool_name in disallowed:
                        tool_id = mcp_tool_id(server_name, tool_name)
                        self._permission.deny_silent(tool_id)

                log.info(
                    "MCP server '%s': %d tools registered",
                    server_name, registered,
                )
                self._tool_counts[server_name] = registered
        finally:
            self._connecting.clear()

    async def stop_all(self) -> None:
        """Gracefully stop all MCP server connections."""
        self._started = False
        self._connecting.clear()

        # Cancel background init if still running
        if self._init_task is not None and not self._init_task.done():
            self._init_task.cancel()
            try:
                await self._init_task
            except (asyncio.CancelledError, Exception):
                pass
            self._init_task = None

        self._registry.unregister_prefix("mcp__")
        self._tool_counts.clear()
        if not self._clients:
            return

        log.info("Stopping %d MCP server(s)...", len(self._clients))
        await self._stop_servers(list(self._clients.values()))
        self._clients.clear()

    async def reconnect(self, server_name: str) -> bool:
        """Attempt to reconnect a specific server."""
        client = self._clients.get(server_name)
        if client is None:
            return False
        return await client.reconnect()

    async def restart_all(self) -> None:
        """Restart all configured server connections and re-register tools."""
        await self.stop_all()
        await self.start_all()

    async def list_tools_for_server(self, server_name: str) -> list[McpToolDef]:
        """List tools from a connected server."""
        client = self._clients.get(server_name)
        if client is None:
            raise McpConnectionError(f"MCP server '{server_name}' is not connected")
        return await client.list_tools()

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict,
    ) -> McpCallResult:
        """Call a tool on a connected MCP server."""
        client = self._clients.get(server_name)
        if client is None:
            raise McpConnectionError(f"MCP server '{server_name}' is not connected")
        if not client.healthy:
            ok = await client.reconnect()
            if not ok:
                raise McpConnectionError(
                    f"MCP server '{server_name}' is unavailable: {client.error_message}"
                )
        return await client.call_tool(tool_name, arguments)

    def statuses(self) -> list[McpRuntimeStatus]:
        """Return runtime status of all configured servers (for UI)."""
        servers = self._server_configs
        if not servers and self._settings is not None:
            servers = self._settings.list_mcp_servers()
        if not servers:
            return []
        result: list[McpRuntimeStatus] = []
        for sc in servers:
            if sc.disabled:
                result.append(McpRuntimeStatus(
                    name=sc.name,
                    status="disabled",
                ))
                continue

            if sc.name in self._connecting:
                result.append(McpRuntimeStatus(
                    name=sc.name,
                    status="connecting",
                ))
                continue

            client = self._clients.get(sc.name)
            if client is None:
                error = self._errors.get(sc.name, "")
                result.append(McpRuntimeStatus(
                    name=sc.name,
                    status="error" if error else "disconnected",
                    error_message=error,
                ))
            else:
                error = self._errors.get(sc.name, "")
                result.append(McpRuntimeStatus(
                    name=sc.name,
                    status="error" if error else client.status,
                    tool_count=self._tool_counts.get(sc.name, 0),
                    error_message=error or client.error_message,
                ))
        return result

    # ── internal ────────────────────────────────────────────────────────

    async def _start_servers(self, configs: list[McpServerConfig]) -> list[tuple[str, McpClient]]:
        """Start multiple servers in parallel, collect successes."""
        async def try_start(sc: McpServerConfig) -> tuple[str, McpClient] | None:
            if sc.name in self._clients:
                return sc.name, self._clients[sc.name]
            retry_config = self._settings.get_retry_config() if self._settings else None
            client = McpClient(sc, retry_config=retry_config)
            try:
                await client.start()
                self._clients[sc.name] = client
                self._errors.pop(sc.name, None)
                return sc.name, client
            except McpConnectionError as e:
                log.warning("MCP server '%s' failed to start: %s", sc.name, e)
                self._errors[sc.name] = str(e)
                return None

        tasks = [try_start(sc) for sc in configs]
        results = await self._gather_safe(tasks)

        successes: list[tuple[str, McpClient]] = []
        for entry in results:
            if entry is not None and not isinstance(entry, BaseException):
                successes.append(entry)
        return successes

    async def _stop_servers(self, clients: list[McpClient]) -> None:
        """Stop multiple servers in parallel."""
        async def safe_stop(client: McpClient) -> None:
            try:
                await client.stop()
            except Exception:
                log.exception("Error stopping MCP server '%s'", client.server_name)

        await self._gather_safe([safe_stop(c) for c in clients])

    @staticmethod
    async def _gather_safe(tasks: list) -> list:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        safe: list = []
        for r in results:
            if isinstance(r, (KeyboardInterrupt, SystemExit)):
                raise r
            if isinstance(r, BaseException):
                safe.append(None)
            else:
                safe.append(r)
        return safe

    @staticmethod
    def _resolve_tool_filter(
        server_config: McpServerConfig | None, allow_mode: bool = True,
    ) -> set[str] | None:
        """Resolve the tools filter from server config.

        If allow_mode=True: returns the set of tool names that ARE allowed.
        If allow_mode=False: returns the set of tool names that are DENIED.
        """
        if server_config is None:
            return None
        tools_field = server_config.tools
        if tools_field is None:
            return None
        if isinstance(tools_field, list):
            # List of tool names that are allowed
            if len(tools_field) == 0:
                return set() if allow_mode else None
            return set(tools_field) if allow_mode else None
        if isinstance(tools_field, dict):
            if allow_mode:
                allowed = {k for k, v in tools_field.items() if v}
                return allowed if allowed else None
            else:
                denied = {k for k, v in tools_field.items() if not v}
                return denied if denied else None
        return None
