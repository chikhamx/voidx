"""MCP delegation helpers for web tools."""

from __future__ import annotations

from typing import Any

from voidx.tools.base import ToolContext, ToolResult


def adapt_mcp_web_arguments(kind: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if kind == "search" and tool == "tavily_search":
        return _adapt_tavily_search_arguments(arguments)
    if kind == "fetch" and tool == "tavily_extract":
        return _adapt_tavily_extract_arguments(arguments)
    return dict(arguments)


def _adapt_tavily_search_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    mapped: dict[str, Any] = {"query": arguments["query"]}

    allowed = arguments.get("allowed_domains")
    if allowed:
        mapped["include_domains"] = allowed

    blocked = arguments.get("blocked_domains")
    if blocked:
        mapped["exclude_domains"] = blocked

    max_results = arguments.get("max_results")
    if max_results:
        mapped["max_results"] = max_results

    return mapped


def _adapt_tavily_extract_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    mapped: dict[str, Any] = {"urls": [arguments["url"]]}

    prompt = arguments.get("prompt")
    if prompt:
        mapped["query"] = prompt

    return mapped


async def call_mcp_web_tool(
    *,
    kind: str,
    settings: Any,
    ctx: ToolContext,
    arguments: dict[str, Any],
    title: str,
) -> ToolResult | None:
    if settings is None or not hasattr(settings, "get_web_tool_route"):
        return None

    route = settings.get_web_tool_route(kind)
    if route.backend != "mcp":
        return None

    if not route.server or not route.tool:
        return ToolResult(
            output=f"Web {kind} is configured for MCP but no server/tool route is set.",
            metadata={"backend": "mcp", "error": True, "kind": kind},
        )

    manager = getattr(ctx, "mcp_manager", None)
    if manager is None:
        return ToolResult(
            output=f"Web {kind} is configured for MCP but no MCP manager is available.",
            metadata={
                "backend": "mcp",
                "error": True,
                "kind": kind,
                "server": route.server,
                "tool": route.tool,
            },
        )

    try:
        adapted_arguments = adapt_mcp_web_arguments(kind, route.tool, arguments)
        result = await manager.call_tool(route.server, route.tool, adapted_arguments)
    except Exception as exc:
        return ToolResult(
            output=f"MCP web {kind} failed via {route.server}/{route.tool}: {exc}",
            metadata={
                "backend": "mcp",
                "error": True,
                "kind": kind,
                "server": route.server,
                "tool": route.tool,
            },
        )

    from voidx.mcp.schema import format_mcp_call_result

    return ToolResult(
        title=title,
        output=format_mcp_call_result(result),
        metadata={
            "backend": "mcp",
            "kind": kind,
            "server": route.server,
            "tool": route.tool,
            "error": result.isError,
        },
    )
