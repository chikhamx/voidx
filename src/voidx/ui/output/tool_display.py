"""Shared tool display helpers for UI output."""

from __future__ import annotations

import re
from typing import Any

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
    "pattern",
    "query",
    "url",
    "urls",
    "uri",
    "uris",
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
    elif tool_name == "grep":
        pattern = raw_args.get("pattern") or raw_args.get("query")
        include = raw_args.get("include")
        value = f"{pattern} in {include}" if pattern and include and short_path_limit is None else pattern
    elif tool_name == "glob":
        value = raw_args.get("pattern") or raw_args.get("query")
    elif tool_name in ("bash", "powershell"):
        value = str(raw_args.get("command") or "").replace("\n", "; ")
    elif tool_name == "git":
        value = raw_args.get("args")
    elif tool_name == "agent":
        value = raw_args.get("agent") or raw_args.get("description")
    elif tool_name == "checkpoint":
        value = raw_args.get("goal")
    elif tool_name in {"webfetch", "websearch"}:
        value = raw_args.get("url") or raw_args.get("query")
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


def mcp_tool_display_name(tool_name: str) -> str:
    match = _MCP_TOOL_ID_RE.match(tool_name)
    if match is None:
        return ""
    server_words = _display_words(match.group("server"))
    tool_words = _display_words(match.group("tool"))
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
