"""Rendering and stripping helpers for MCP gateway current-turn context.

Mirrors voidx.skills.context: `mcp load` output is injected as a tool result
for the current turn and compressed in historical messages via a marker.
"""

from __future__ import annotations

import json
import re
from typing import Any

from voidx.mcp.schema import McpToolDef
from voidx.mcp.schema_summary import summarize_schema

MCP_TOOL_CONTEXT_MARKER = "VOIDX_MCP_TOOL_CONTEXT"
MCP_TOOL_CONTEXT_STRIPPED_MARKER = "VOIDX_MCP_TOOL_CONTEXT_STRIPPED"

_MCP_SERVER_HEADER_RE = re.compile(r"^## MCP Server:\s*(?P<name>.+?)\s*$", re.MULTILINE)
_MCP_TOOL_LINE_RE = re.compile(r"^-\s+(?P<tool>[^\s:]+)", re.MULTILINE)
_MCP_TOOL_CONTEXT_MARKER_RE = re.compile(
    rf"(?m)^{re.escape(MCP_TOOL_CONTEXT_MARKER)}[ \t]*(?:\r?\n|$)"
)


def render_mcp_server_summary(
    server: str,
    *,
    status: str,
    description: str = "",
    instructions: str = "",
    server_info: dict[str, Any] | None = None,
) -> str:
    lines = [
        MCP_TOOL_CONTEXT_MARKER,
        "Scope: current-turn",
        "",
        f"## MCP Server: {server}",
    ]
    if description.strip():
        lines.append(f"Summary: {description.strip()}")
    lines.append("")
    lines.append(f'Use `mcp(op="load", server="{server}")` to expand tools and parameters.')
    return "\n".join(lines)


def render_mcp_tool_context(server: str, status: str, tools: list[McpToolDef]) -> str:
    """Render one MCP server's tools as current-turn context for the model."""
    lines = [
        MCP_TOOL_CONTEXT_MARKER,
        "Scope: current-turn",
        "",
        f"## MCP Server: {server}",
        f"Status: {status}",
        "",
        "Tools:",
    ]
    if not tools:
        lines.append("No tools available for this server.")
    for tool_def in tools:
        lines.extend(_render_tool_lines(server, tool_def))
    return "\n".join(lines)


def strip_mcp_tool_context(content: Any) -> Any:
    if isinstance(content, str):
        return _strip_mcp_tool_context_text(content)
    if isinstance(content, list):
        changed = False
        stripped_items: list[Any] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                if isinstance(text, str):
                    stripped = _strip_mcp_tool_context_text(text)
                    if stripped != text:
                        item = {**item, "text": stripped}
                        changed = True
            stripped_items.append(item)
        return stripped_items if changed else content
    return content


def _render_tool_lines(server: str, tool_def: McpToolDef) -> list[str]:
    description = tool_def.description.strip() or f"MCP tool from {server}"
    lines = [f"- {tool_def.name}: {description}"]

    summary = summarize_schema(tool_def.inputSchema)
    if summary.required_names:
        lines.append(f"  Required: {', '.join(summary.required_names)}")
    if summary.optional_names:
        lines.append(f"  Optional: {', '.join(summary.optional_names)}")
    if summary.truncated:
        lines.append(f"  Note: parameter list truncated; load tool '{tool_def.name}' for details.")
    lines.append("  Example:")
    lines.append(f"    {_example_call(server, tool_def)}")
    return lines


def _example_call(server: str, tool_def: McpToolDef) -> str:
    summary = summarize_schema(tool_def.inputSchema)
    example_args = json.dumps(
        {name: _placeholder_for(summary, name) for name in summary.required_names},
        ensure_ascii=False,
    )
    return f'mcp(op="call", server="{server}", tool="{tool_def.name}", arguments={example_args})'


def _placeholder_for(summary, name: str) -> Any:
    if name not in summary.required_names:
        return "..."
    if name.endswith("s"):
        return []
    if name.startswith(("is_", "has_", "should_")):
        return False
    if name in {"query", "input", "text", "prompt", "message", "url", "path", "server", "tool"}:
        return "..."
    return "..."


def _strip_mcp_tool_context_text(text: str) -> str:
    if _MCP_TOOL_CONTEXT_MARKER_RE.search(text) is None:
        return text
    parts = _MCP_TOOL_CONTEXT_MARKER_RE.split(text)
    prefix = parts[0]
    replacements = [_stripped_summary(block) for block in parts[1:]]
    replacement = "\n\n".join(replacements)
    if prefix.strip():
        return f"{prefix.rstrip()}\n\n{replacement}"
    return replacement


def _stripped_summary(block: str) -> str:
    server = _extract_server_name(block)
    tools = _tool_names(block)
    lines = [MCP_TOOL_CONTEXT_STRIPPED_MARKER]
    if server and tools:
        lines.append(f"- MCP server context omitted: {server}; tools: {', '.join(tools)}")
    elif server:
        lines.append(f"- MCP server context omitted: {server}")
    elif tools:
        lines.append(f"- MCP server context omitted; tools: {', '.join(tools)}")
    else:
        lines.append("- MCP server context omitted")
    return "\n".join(lines)


def _extract_server_name(block: str) -> str:
    match = _MCP_SERVER_HEADER_RE.search(block)
    return match.group("name").strip() if match else ""


def _tool_names(block: str) -> list[str]:
    return [match.group("tool").strip() for match in _MCP_TOOL_LINE_RE.finditer(block)]
