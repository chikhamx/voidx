"""Shared tool display helpers for UI output."""

from __future__ import annotations

import json
import re
from typing import Any

from voidx.ui.output.agent_display import subagent_display_name
from voidx.ui.output.dock.formatting import short_path
from voidx.ui.output.manage_display import manage_display

_MCP_TOOL_ID_RE = re.compile(r"^mcp__(?P<server>.+?)__(?P<tool>.+)_(?P<hash>[0-9a-f]{8})$", re.IGNORECASE)
_ACRONYMS = {
    "api": "API",
    "http": "HTTP",
    "id": "ID",
    "json": "JSON",
    "mcp": "MCP",
    "sse": "SSE",
    "uri": "URI",
    "url": "URL",
}
_GENERIC_DISPLAY_ARG_KEYS = (
    "file_path",
    "path",
    "url",
    "urls",
    "uri",
    "uris",
    "pattern",
    "query",
    "command",
    "name",
    "input",
    "text",
    "prompt",
    "keywords",
    "resource",
)


def extract_tool_display_value(
    tool_name: str,
    raw_args: dict[str, Any],
    args: str,
    *,
    short_path_limit: int | None = None,
) -> str:
    value: object = ""
    if tool_name in {"read", "write", "replace", "edit", "lsp"}:
        value = raw_args.get("file_path") or raw_args.get("path")
    elif tool_name == "manage":
        _action, value = manage_display(raw_args)
    elif tool_name == "search":
        pattern = raw_args.get("pattern") or raw_args.get("query")
        include = raw_args.get("include")
        value = f"{pattern} in {include}" if pattern and include and short_path_limit is None else pattern
    elif tool_name == "find":
        value = raw_args.get("pattern") or raw_args.get("query")
    elif tool_name in ("bash", "powershell"):
        value = str(raw_args.get("command") or "").replace("\n", "; ")
    elif tool_name == "git":
        value = raw_args.get("args")
    elif tool_name == "agent":
        value = _agent_display_value(raw_args)
    elif tool_name == "checkpoint":
        value = raw_args.get("goal")
    elif tool_name in {"webfetch", "websearch"}:
        value = raw_args.get("url") or raw_args.get("query")
    elif tool_name == "mcp":
        value = mcp_gateway_display_value(raw_args)
    elif raw_args:
        value = _first_display_arg(raw_args)
    if not _has_display_value(value):
        value = strip_rich_markup(args)
    result = _format_display_value(value)
    if not result:
        return ""
    if short_path_limit is not None:
        return short_path(result, limit=short_path_limit)
    return result


def _agent_display_value(raw_args: dict[str, Any]) -> object:
    action = str(raw_args.get("action") or "spawn").strip().lower()
    if action in {"wait", "cancel"}:
        run_id = str(raw_args.get("target_run_id") or "").strip()
        return subagent_display_name(run_id) if run_id else ""
    return raw_args.get("name") or raw_args.get("description") or ""

def mcp_tool_display_name(tool_name: str) -> str:
    match = _MCP_TOOL_ID_RE.match(tool_name)
    if match is None:
        return ""
    return _mcp_action_display_name(match.group("server"), match.group("tool"))


_MCP_GATEWAY_VALUE_KEYS = ("query", "url", "urls", "path", "pattern", "name", "text")


def mcp_gateway_tool_name(raw_args: dict[str, Any]) -> str:
    """Display name for the fixed `mcp` gateway tool, based on the op."""
    op = str(raw_args.get("op") or "")
    if op == "list":
        return "MCP List"
    if op == "load":
        return "MCP Load"
    if op == "call":
        name = _mcp_action_display_name(
            str(raw_args.get("server") or ""),
            str(raw_args.get("tool") or ""),
        )
        return name or "MCP Call"
    return "MCP"


def mcp_gateway_display_value(raw_args: dict[str, Any]) -> object:
    """Display value for gateway calls, parsed from the arguments JSON string."""
    op = str(raw_args.get("op") or "")
    if op == "load":
        return str(raw_args.get("server") or "")
    if op != "call":
        return ""
    server = str(raw_args.get("server") or "")
    tool = str(raw_args.get("tool") or "")
    parsed = _parse_gateway_arguments(raw_args.get("arguments"))
    if parsed is None:
        return f"{server}/{tool}" if server or tool else ""
    for key in _MCP_GATEWAY_VALUE_KEYS:
        value = parsed.get(key)
        if _has_display_value(value):
            return value
    return ""


def _parse_gateway_arguments(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _mcp_action_display_name(server: str, tool: str) -> str:
    server_words = _display_words(server)
    tool_words = _display_words(tool)
    if not tool_words:
        return _title_words(server_words)
    if server_words and tool_words[: len(server_words)] == server_words:
        return _title_words(tool_words)
    return _title_words([*server_words, *tool_words])


def _display_words(value: str) -> list[str]:
    return [part.lower() for part in re.split(r"[^A-Za-z0-9]+", value) if part]


def _title_words(words: list[str]) -> str:
    return " ".join(_ACRONYMS.get(word, word.capitalize()) for word in words)


def _first_display_arg(raw_args: dict[str, Any]) -> object:
    for key in _GENERIC_DISPLAY_ARG_KEYS:
        value = raw_args.get(key)
        if _has_display_value(value):
            return value
    return ""


def _has_display_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _format_display_value(value: object) -> str:
    if isinstance(value, (list, tuple)):
        parts = [_format_display_value(item) for item in value]
        parts = [part for part in parts if part]
        if not parts:
            return ""
        suffix = f" +{len(parts) - 1} more" if len(parts) > 1 else ""
        return f"{parts[0]}{suffix}"
    return " ".join(str(value).split())


def strip_rich_markup(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\[/?[A-Za-z0-9_#= .:-]+\]", "", text)
    text = text.strip()
    if "=" in text:
        text = text.split("=", 1)[1].strip()
    return text.strip("\"'")
