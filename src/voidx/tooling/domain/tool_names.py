"""Canonical tool-name normalization shared by policy layers."""

from __future__ import annotations


_TOOL_ALIASES = {
    "Read": "read",
    "Write": "manage",
    "Edit": "replace",
    "Delete": "replace",
    "MultiEdit": "replace",
    "multiEdit": "replace",
    "multi_edit": "replace",
    "Find": "find",
    "Search": "search",
    "Bash": "bash",
    "PowerShell": "powershell",
    "Git": "git",
    "git": "git",
    "Agent": "agent",
    "TodoWrite": "todo",
    "Todo": "todo",
    "WebFetch": "webfetch",
    "WebSearch": "websearch",
    "read_file": "read",
    "write_file": "manage",
    "edit_file": "replace",
    "shell": "bash",
    "readfile": "read",
    "writefile": "manage",
    "LspDiagnostics": "lsp",
    "LspSymbols": "lsp",
    "LspDefinition": "lsp",
    "LspReferences": "lsp",
    "CompactContext": "compact",
    "find": "find",
    "search": "search",
    "edit": "replace",
    "insert": "write",
    "append": "write",
    "delete": "replace",
    "line": "write",
}


def canonical_tool_name(tool: str) -> str:
    return _TOOL_ALIASES.get(tool, _TOOL_ALIASES.get(tool.lower(), tool))
