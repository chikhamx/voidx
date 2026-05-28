"""Slash command palette — Claude Code style. / triggers selectable command list."""

from __future__ import annotations

COMMANDS: list[tuple[str, str]] = [
    ("/clear", "Start a new session with empty context"),
    ("/list", "List saved sessions"),
    ("/resume", "Resume a session by ID"),
    ("/title", "Set session title"),
    ("/model", "Switch LLM model — /model to list, /model <name> to switch"),
    ("/plan", "Enter plan mode (write/edit blocked)"),
    ("/unplan", "Exit plan mode"),
    ("/allow", "Allow a tool for this session"),
    ("/deny", "Deny a tool for this session"),
    ("/permissions", "Show current permission rules"),
    ("/compact", "Manually trigger context compaction"),
    ("/diff", "Show git working tree diff with syntax highlighting"),
    ("/exit", "Exit voidx"),
    ("/help", "Show all commands"),
]

def filter_commands(prefix: str) -> list[tuple[str, str]]:
    """Filter commands by prefix. Returns (name, description) pairs.

    Matches both when the input *is* a prefix of a command (e.g. ``/res``
    matches ``/resume``) and when the input *starts with* a command
    (e.g. ``/resume abc123`` matches ``/resume``).
    """
    p = prefix.lower()
    return [(n, d) for n, d in COMMANDS
            if n.lower().startswith(p) or p.startswith(n.lower())]
