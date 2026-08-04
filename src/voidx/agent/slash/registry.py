"""Slash command registry — single source of truth for dispatch and help.

Add a new top-level command in exactly one place: ``SLASH_COMMANDS``.
The dispatch table (``SlashHandler.dispatch``), ``/help`` output, and the
command palette catalog (``voidx.ui.commands.COMMANDS``) all derive from it.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlashCommand:
    name: str  # "/chat"
    desc: str  # shown in /help and the command palette
    method: str  # handler method on SlashHandler
    arg: str = "args"  # "none" | "args" | "inp" — what dispatch passes to the handler


SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("/allow", "Allow a tool for this session", "_cmd_allow", "args"),
    SlashCommand("/bocha", "Configure Bocha API key for web search", "_bocha", "args"),
    SlashCommand("/chat", "Start a new chat session", "_chat_shortcut", "args"),
    SlashCommand("/coding", "Start a new coding session", "_coding_shortcut", "args"),
    SlashCommand("/clear", "Start a new session with empty context", "_clear", "none"),
    SlashCommand("/code-ide", "Choose app for opening changed files", "_code_ide", "args"),
    SlashCommand("/compact", "Manually trigger context compaction", "_cmd_compact", "none"),
    SlashCommand("/debug", "Toggle verbose step/tool output", "_debug", "args"),
    SlashCommand("/deny", "Deny a tool for this session", "_cmd_deny", "args"),
    SlashCommand("/diff", "Show git working tree diff with syntax highlighting", "_show_diff", "none"),
    SlashCommand("/exit", "Exit voidx", "_noop", "none"),
    SlashCommand("/goal", "Switch to goal mode or manage the current goal", "_goal", "args"),
    SlashCommand("/guide", "Add guidance to the running agent turn", "_guide", "args"),
    SlashCommand("/help", "Show all commands", "_show_help", "none"),
    SlashCommand("/init", "Generate AGENTS.md for this project", "_init", "args"),
    SlashCommand("/lang", "Set response language preference", "_lang", "args"),
    SlashCommand("/list", "List saved sessions", "_list_sessions", "none"),
    SlashCommand("/log", "Toggle LLM logging", "_log", "args"),
    SlashCommand("/loop", "Switch to loop mode or manage the current loop", "_loop", "args"),
    SlashCommand("/lsp", "Manage language servers", "_lsp", "args"),
    SlashCommand("/mcp", "Manage MCP servers", "_mcp", "args"),
    SlashCommand("/model", "Switch configured model", "_dispatch_model", "args"),
    SlashCommand("/paste", "Paste an image from the clipboard", "_paste_clipboard_image", "none"),
    SlashCommand("/permission", "Choose permission mode", "_permission_mode", "args"),
    SlashCommand("/permissions", "Show current permission rules", "_cmd_permissions", "none"),
    SlashCommand("/plan", "Enter plan mode (writes and write-capable bash blocked)", "_cmd_plan", "none"),
    SlashCommand("/quit", "Exit voidx", "_noop", "none"),
    SlashCommand("/resume", "Resume a session (select from list or specify ID)", "_resume", "inp"),
    SlashCommand("/rollback", "Revert file changes from the current turn", "_rollback", "none"),
    SlashCommand("/session", "Manage sessions (new/list/resume/del)", "_session", "args"),
    SlashCommand("/skills", "Manage local skills", "_skills", "args"),
    SlashCommand("/tavily", "Configure Tavily API key for web search", "_tavily", "args"),
    SlashCommand("/title", "Set session title", "_set_title", "inp"),
    SlashCommand("/tone", "Set response tone preference", "_tone", "args"),
    SlashCommand("/unplan", "Return to auto mode", "_cmd_unplan", "none"),
    SlashCommand("/upgrade", "Check for voidx updates", "_upgrade", "args"),
    SlashCommand("/usage", "Show token usage for this session", "_usage", "none"),
)

REGISTRY: dict[str, SlashCommand] = {spec.name: spec for spec in SLASH_COMMANDS}
