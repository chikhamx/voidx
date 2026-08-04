"""Slash command palette — Claude Code style. / triggers selectable command list.

Top-level commands derive from ``voidx.agent.slash.registry.SLASH_COMMANDS``
so a new command is registered in exactly one place and automatically appears
in ``/help``, the command palette, and the gateway catalog. Sub-command
entries (e.g. ``/mcp del``) have no dedicated dispatch handler and stay here.
"""
from __future__ import annotations

from voidx.agent.slash.registry import SLASH_COMMANDS

_COMMAND_EXTRA: list[tuple[str, str]] = [
    ("/bocha delete", "Delete Bocha API key"),
    ("/bocha set", "Set Bocha API key for web search"),
    ("/bocha show", "Show Bocha API key status"),
    ("/code-ide status", "Show detected code IDEs"),
    ("/debug off", "Disable verbose step/tool output"),
    ("/debug on", "Enable verbose step/tool output"),
    ("/init force", "Regenerate AGENTS.md even if it already exists"),
    ("/log diagnostic", "Toggle diagnostic logging"),
    ("/log exchange", "Toggle exchange logging"),
    ("/loop status", "Show current loop status"),
    ("/loop stop", "Stop the current loop"),
    ("/lsp doctor", "Check installed language servers"),
    ("/lsp restart", "Restart language servers"),
    ("/lsp servers", "List configured LSP servers"),
    ("/lsp status", "Show LSP server status"),
    ("/mcp auto", "Mark an MCP server for auto-discovery"),
    ("/mcp del", "Remove an MCP server"),
    ("/mcp disable", "Disable an MCP server"),
    ("/mcp enable", "Enable an MCP server"),
    ("/mcp list", "List configured MCP servers"),
    ("/mcp manual", "Mark an MCP server for manual discovery only"),
    ("/mcp new", "Configure a new MCP server"),
    ("/mcp restart", "Restart an MCP server"),
    ("/mcp test", "Test an MCP server connection"),
    ("/mcp tools", "Show MCP server tools"),
    ("/model ctx", "Set context window size"),
    ("/model del", "Remove a profile"),
    ("/model list", "Show configured model details"),
    ("/model new", "Create or update a model profile"),
    ("/model reasoning", "Set reasoning effort level"),
    ("/model switch", "Switch to a configured provider"),
    ("/model test", "Test a provider's connectivity"),
    ("/permission ai_approval", "AI approval pre-screens dangerous tools; optionally add a profile name"),
    ("/permission full_access", "Allow most operations; ask for extreme risk"),
    ("/permission project_trusted", "Allow workspace edits; ask for broader risk"),
    ("/permission read_only", "Ask for writes and block unsafe operations"),
    ("/permission safe", "Ask before writes or risky commands"),
    ("/session del --dry-run", "Preview session deletion candidates"),
    ("/session del", "Delete old saved sessions"),
    ("/session list", "List saved sessions"),
    ("/session new", "Start a new session with empty context"),
    ("/session resume", "Resume a saved session"),
    ("/skills auto", "Set a skill to auto-trigger"),
    ("/skills disable", "Disable a skill"),
    ("/skills enable", "Enable a skill"),
    ("/skills list", "List local skills"),
    ("/skills manual", "Set a skill to manual trigger"),
    ("/skills paths", "Show skill directory paths"),
    ("/skills show", "Show a skill's content"),
    ("/tavily delete", "Delete Tavily API key"),
    ("/tavily set", "Set Tavily API key for web search"),
    ("/tavily show", "Show Tavily API key status"),
    ("/title auto", "Regenerate session title"),
    ("/upgrade check", "Check PyPI for a newer voidx version"),
    ("/upgrade now", "Upgrade voidx in the current Python environment"),
    ("/upgrade off", "Disable startup update checks"),
    ("/upgrade on", "Enable startup update checks"),
    ("/upgrade status", "Show update check status"),
]

COMMANDS: list[tuple[str, str]] = sorted(
    [(spec.name, spec.desc) for spec in SLASH_COMMANDS] + _COMMAND_EXTRA,
    key=lambda pair: pair[0],
)

def filter_commands(prefix: str) -> list[tuple[str, str]]:
    """Filter commands by prefix. Returns (name, description) pairs.

    Matches both when the input *is* a prefix of a command (e.g. ``/res``
    matches ``/resume``) and when the input *starts with* a command
    (e.g. ``/resume abc123`` matches ``/resume``).
    """
    p = prefix.lower()
    return [(n, d) for n, d in COMMANDS
            if n.lower().startswith(p) or p.startswith(n.lower())]
