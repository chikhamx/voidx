"""MCP gateway tool — one stable `mcp` tool covering discovery, load, and call.

The gateway keeps the bound tool list stable regardless of how many MCP
servers are connected: the model discovers servers via `list`, loads parameter
details via `load` (current-turn context, stripped from history), and executes
real tools via `call`. All three ops read from the shared McpCatalog so the
gateway, direct wrappers, and UI never disagree about availability.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from voidx.mcp.domain.errors import McpConnectionError
from voidx.mcp.context import render_mcp_tool_context
from voidx.mcp.descriptions import configured_server_description
from voidx.mcp.schema import McpToolDef, format_mcp_call_result
from voidx.mcp.validation import validate_mcp_arguments
from voidx.tooling.ports.mcp import McpGateway
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.domain.arguments import (
    is_nullish_tool_value,
    keep_tool_args,
    normalize_nullable_tool_fields,
)
from voidx.tooling.domain.schema import model_to_json_schema


_MAX_LIST_SERVERS = 50


class McpInput(BaseModel):
    """Discover, load, and call Model Context Protocol (MCP) tools."""

    op: Literal["list", "load", "call"] = Field(
        description=(
            "Operation: 'list' (discover MCP servers and tool bundles), "
            "'load' (fetch parameter details for a server or tool), "
            "'call' (execute a real MCP tool)."
        )
    )
    server: str | None = Field(default=None, description="MCP server name. Required for op=load and op=call.")
    tool: str | None = Field(default=None, description="MCP tool name. Required for op=call; optional for op=load.")
    arguments: dict[str, Any] | None = Field(
        default=None,
        strict=True,
        description="Tool arguments for op=call. Pass a JSON object, not a serialized JSON string.",
    )
    query: str | None = Field(default=None, description="Optional filter for op=list.")

    @field_validator("arguments", mode="before")
    @classmethod
    def require_arguments_object(cls, value: Any) -> Any:
        if value is not None and not isinstance(value, dict):
            raise ValueError("arguments must be a JSON object")
        return value


def _normalize_mcp_args(args: Any) -> Any:
    if not isinstance(args, dict):
        return args
    op = str(args.get("op") or "").strip().lower()
    if op == "list":
        return keep_tool_args(args, {"op", "query"})
    if op == "load":
        normalized = keep_tool_args(args, {"op", "server", "tool"})
        normalized = normalize_nullable_tool_fields(normalized, "server", "tool")
        if is_nullish_tool_value(normalized.get("tool")):
            normalized.pop("tool", None)
        return normalized
    if op == "call":
        return normalize_nullable_tool_fields(
            keep_tool_args(args, {"op", "server", "tool", "arguments"}),
            "server",
            "tool",
        )
    return args


class McpGatewayTool:
    id = "mcp"
    description = (
        "Discover and use Model Context Protocol (MCP) servers through a stable gateway.\n\n"
        "- `mcp(op=\"list\")` returns semantic server summaries; it does not load tool documentation.\n"
        "- When a server is relevant, use `mcp(op=\"load\", server=\"...\")` before calling it.\n"
        "- `mcp(op=\"load\")` may target a whole server or one tool and returns current-turn context.\n"
        "- `mcp(op=\"call\", ...)` executes a real MCP tool; pass arguments as a JSON object.\n"
        "- Never invent server names, tool names, or parameters; list or load when uncertain."
    )

    def scoped(self, allowed_servers: set[str] | frozenset[str]) -> "ScopedMcpGatewayTool":
        """Return an autonomous view restricted to the provided MCP servers."""
        return ScopedMcpGatewayTool(self._gateway, allowed_servers)

    def __init__(self, gateway: McpGateway | None) -> None:
        super().__init__()
        self._gateway = gateway

    def parameters_schema(self) -> dict[str, Any]:
        schema = model_to_json_schema(McpInput)
        schema["properties"]["arguments"]["additionalProperties"] = True
        return schema

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        args = _normalize_mcp_args(args)
        try:
            inp = McpInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        if self._gateway is None:
            return ToolResult(output="MCP manager not available.", metadata={"error": True, "reason": "mcp_unavailable"})
        if inp.op == "list":
            return self._execute_list(inp)
        if inp.op == "load":
            return self._execute_load(inp)
        return await self._execute_call(inp)

    # ── list ────────────────────────────────────────────────────────────

    def _execute_list(self, inp: McpInput) -> ToolResult:
        statuses = {s.name: s for s in self._gateway.statuses()}
        catalog = {e.name: e for e in self._gateway.catalog_snapshot()}
        names = sorted(set(statuses) | set(catalog))
        if not names:
            return ToolResult(
                output="No MCP servers configured. Add servers to `mcpServers` in settings to use MCP tools.",
            )

        lines: list[str] = []
        matched = 0
        for name in names:
            status = statuses.get(name)
            entry = catalog.get(name)
            tools = list(entry.tools) if entry is not None else []
            config = self._gateway.server_config(name)
            if inp.query and not _matches_query(inp.query, name, status, tools, config, entry):
                continue
            matched += 1
            if matched > _MAX_LIST_SERVERS:
                continue
            lines.extend(self._list_lines(name, status, tools, config, entry))

        if matched == 0:
            return ToolResult(
                output=f"No MCP servers match query '{inp.query}'. Run mcp(op=\"list\") to see all servers.",
            )
        header = f"MCP servers ({matched})"
        if matched > _MAX_LIST_SERVERS:
            header += f", showing first {_MAX_LIST_SERVERS}"
        return ToolResult(
            output=f"{header}:\n" + "\n".join(lines),
            metadata={"server_count": matched},
        )

    @staticmethod
    def _list_lines(name: str, status, tools: list[McpToolDef], config=None, entry=None) -> list[str]:
        state = status.status if status is not None else "unknown"
        count = len(tools) if tools else (status.tool_count if status is not None else 0)
        summary = ""
        if entry is not None:
            summary = (entry.description or "").strip()
        if config is not None:
            summary = summary or configured_server_description(config)
        headline = f"- {name}"
        if summary:
            headline += f": {summary}"
        details = f"  Status: {state}; {count} tools available."
        if status is not None and status.error_message:
            details += f" Error: {status.error_message}."
        load = f'  Load: mcp(op="load", server="{name}")'
        return [headline, details, load]

    # ── load ────────────────────────────────────────────────────────────

    def _execute_load(self, inp: McpInput) -> ToolResult:
        if not inp.server:
            return ToolResult(
                output='mcp(op="load") requires a server name. Run mcp(op="list") to discover servers.',
                metadata={"error": True},
            )
        status = next((s for s in self._gateway.statuses() if s.name == inp.server), None)
        if status is None:
            known = sorted(s.name for s in self._gateway.statuses())
            hint = f" Available servers: {', '.join(known)}." if known else ""
            return ToolResult(
                output=f"Unknown MCP server '{inp.server}'.{hint} Run mcp(op=\"list\") to discover servers.",
                metadata={"error": True, "error_kind": "unknown_server"},
            )

        entry = next((e for e in self._gateway.catalog_snapshot() if e.name == inp.server), None)
        tools = list(entry.tools) if entry is not None else []
        if inp.tool:
            tool_def = next((t for t in tools if t.name == inp.tool), None)
            if tool_def is None:
                available = ", ".join(t.name for t in tools) or "none discovered"
                return ToolResult(
                    output=(
                        f"Unknown tool '{inp.tool}' on MCP server '{inp.server}'. "
                        f"Available tools: {available}."
                    ),
                    metadata={"error": True, "error_kind": "unknown_tool", "server": inp.server},
                )
            tools = [tool_def]

        output = render_mcp_tool_context(inp.server, status.status, tools)
        return ToolResult(
            output=output,
            metadata={
                "server": inp.server,
                "tool_names": [t.name for t in tools],
                "schema_hash": _schema_hash(tools),
                "truncated": False,
            },
        )

    # ── call ────────────────────────────────────────────────────────────

    async def _execute_call(self, inp: McpInput) -> ToolResult:
        if not inp.server or not inp.tool:
            return ToolResult(
                output=(
                    'mcp(op="call") requires server and tool. '
                    'Run mcp(op="list") to discover servers, then mcp(op="load", server="...", tool="...") for details.'
                ),
                metadata={"error": True},
            )
        if inp.arguments is None:
            return ToolResult(
                output=(
                    'mcp(op="call") requires arguments as a JSON object. '
                    'Pass arguments={} when the target tool accepts no parameters.'
                ),
                metadata={"error": True},
            )

        status = next((s for s in self._gateway.statuses() if s.name == inp.server), None)
        if status is None:
            known = sorted(s.name for s in self._gateway.statuses())
            hint = f" Available servers: {', '.join(known)}." if known else ""
            return ToolResult(
                output=f"Unknown MCP server '{inp.server}'.{hint} Run mcp(op=\"list\") to discover servers.",
                metadata={"error": True, "error_kind": "unknown_server"},
            )
        if status.status != "connected":
            return ToolResult(
                output=(
                    f"MCP server '{inp.server}' is not connected (status: {status.status}"
                    f"{': ' + status.error_message if status.error_message else ''}). Retry later."
                ),
                metadata={"error": True, "error_kind": "server_unavailable", "server": inp.server},
            )

        tool_def = self._gateway.tool_def(inp.server, inp.tool)
        if tool_def is None:
            entry = next((e for e in self._gateway.catalog_snapshot() if e.name == inp.server), None)
            available = ", ".join(t.name for t in entry.tools) if entry is not None and entry.tools else "none discovered"
            return ToolResult(
                output=(
                    f"Unknown tool '{inp.tool}' on MCP server '{inp.server}'. "
                    f"Available tools: {available}. Run mcp(op=\"load\", server=\"{inp.server}\") for details."
                ),
                metadata={"error": True, "error_kind": "unknown_tool", "server": inp.server},
            )

        validated = validate_mcp_arguments(inp.arguments, tool_def.inputSchema)
        if validated.error is not None:
            return ToolResult(
                output=(
                    f"MCP call failed: {validated.error.message}\n"
                    f'Run mcp(op="load", server="{inp.server}", tool="{inp.tool}") for parameter details.'
                ),
                metadata={
                    "error": True,
                    "error_kind": validated.error.kind,
                    "server": inp.server,
                    "tool": inp.tool,
                },
            )

        try:
            result = await self._gateway.call_tool(inp.server, inp.tool, validated.arguments or {})
        except McpConnectionError as e:
            return ToolResult(
                output=f"MCP server '{inp.server}' is unavailable: {e}",
                metadata={"error": True, "error_kind": "server_unavailable", "server": inp.server, "tool": inp.tool},
            )
        except Exception as e:
            return ToolResult(
                output=f"MCP tool '{inp.tool}' error: {e}",
                metadata={"error": True, "error_kind": "mcp_error", "server": inp.server, "tool": inp.tool},
            )

        meta: dict[str, Any] = {"server": inp.server, "tool": inp.tool}
        if result.isError:
            meta["error"] = True
            meta["error_kind"] = "mcp_error"
        return ToolResult(output=format_mcp_call_result(result), metadata=meta)


class _ScopedMcpGateway:
    """Read/call view that exposes only an autonomous profile's allowlist."""

    def __init__(self, gateway: McpGateway, allowed_servers: frozenset[str]) -> None:
        self._gateway = gateway
        self._allowed_servers = allowed_servers

    def statuses(self) -> list[object]:
        return [status for status in self._gateway.statuses() if status.name in self._allowed_servers]

    def catalog_snapshot(self) -> list[object]:
        return [entry for entry in self._gateway.catalog_snapshot() if entry.name in self._allowed_servers]

    def server_config(self, name: str) -> object | None:
        if name not in self._allowed_servers:
            return None
        return self._gateway.server_config(name)

    def tool_def(self, server: str, tool: str) -> object | None:
        if server not in self._allowed_servers:
            return None
        return self._gateway.tool_def(server, tool)

    async def call_tool(self, server: str, tool: str, arguments: dict) -> object:
        if server not in self._allowed_servers:
            raise ValueError(f"MCP server '{server}' is outside the autonomous profile scope")
        return await self._gateway.call_tool(server, tool, arguments)


class ScopedMcpGatewayTool(McpGatewayTool):
    """MCP gateway view restricted to an autonomous profile's server allowlist."""

    description = (
        "Discover and use only the allowlisted Model Context Protocol (MCP) servers "
        "for the current autonomous profile.\n\n"
        "- `mcp(op=\"list\")` lists only allowlisted servers.\n"
        "- `mcp(op=\"load\", server=\"...\")` loads only an allowlisted server or tool.\n"
        "- `mcp(op=\"call\", server=\"...\", tool=\"...\", ...)` requires non-empty server and tool "
        "and rejects servers outside the allowlist."
    )

    def __init__(
        self,
        gateway: McpGateway | None,
        allowed_servers: set[str] | frozenset[str],
    ) -> None:
        self._allowed_servers = frozenset(allowed_servers)
        scoped_gateway = (
            _ScopedMcpGateway(gateway, self._allowed_servers)
            if gateway is not None
            else None
        )
        super().__init__(scoped_gateway)

    def scoped(self, allowed_servers: set[str] | frozenset[str]) -> "ScopedMcpGatewayTool":
        return ScopedMcpGatewayTool(
            self._gateway,
            self._allowed_servers.intersection(allowed_servers),
        )

    def parameters_schema(self) -> dict[str, Any]:
        schema = super().parameters_schema()
        schema["properties"]["server"]["description"] = (
            "Allowlisted MCP server name for the current autonomous profile. "
            "Required for op=load and op=call."
        )
        schema["properties"]["tool"]["description"] = (
            "MCP tool name. A non-empty value is required for op=call; optional for op=load."
        )
        return schema

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        normalized = _normalize_mcp_args(args)
        if isinstance(normalized, dict) and normalized.get("op") in {"load", "call"}:
            server = normalized.get("server")
            if server and server not in self._allowed_servers:
                return ToolResult(
                    output=(
                        f"MCP server '{server}' is not allowlisted for the current autonomous profile. "
                        'Run mcp(op="list") to discover allowed servers.'
                    ),
                    metadata={
                        "error": True,
                        "error_kind": "server_not_allowed",
                        "server": server,
                    },
                )
        return await super().execute(normalized, ctx)


def _matches_query(query: str, name: str, status, tools: list[McpToolDef], config=None, entry=None) -> bool:
    needle = query.strip().lower()
    if not needle:
        return True
    if needle in name.lower():
        return True
    if config is not None and (
        needle in config.description.lower() or needle in config.source.lower()
    ):
        return True
    if entry is not None and needle in (entry.description or "").lower():
        return True
    if status is not None and needle in status.error_message.lower():
        return True
    return any(
        needle in t.name.lower() or needle in (t.description or "").lower()
        for t in tools
    )


def _schema_hash(tools: list[McpToolDef]) -> str:
    payload = json.dumps(
        [{ "name": t.name, "inputSchema": t.inputSchema } for t in tools],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
