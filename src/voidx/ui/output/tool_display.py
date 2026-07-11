"""Shared tool display helpers for UI output."""

from __future__ import annotations

import re
from typing import Any

from voidx.ui.output.dock.formatting import short_path
from voidx.ui.output.manage_display import manage_display


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
        for key in ("file_path", "path", "pattern", "query", "url", "command", "name"):
            if raw_args.get(key):
                value = raw_args[key]
                break
    if not value:
        value = strip_rich_markup(args)
    if not value:
        return ""
    result = " ".join(str(value).split())
    if short_path_limit is not None:
        return short_path(result, limit=short_path_limit)
    return result


def strip_rich_markup(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\[/?[A-Za-z0-9_#= .:-]+\]", "", text)
    text = text.strip()
    if "=" in text:
        text = text.split("=", 1)[1].strip()
    return text.strip("\"'")
