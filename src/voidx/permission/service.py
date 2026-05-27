"""Permission service — built-in defaults + session whitelist + interactive ask.

Default rules: read-only tools are auto-allowed, write/bash/task need approval.

Session whitelist: once user says "always", the tool is remembered.
Manage via /allow <tool>, /deny <tool>, /permissions commands.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from pydantic import BaseModel

from voidx.permission.schema import Action, Rule, Ruleset
from voidx.permission.evaluate import evaluate
from voidx.ui.console import VoidConsole

ui = VoidConsole()

# ── built-in defaults ─────────────────────────────────────────────────────

DEFAULT_RULES: Ruleset = [
    # Read-only tools: always allow
    Rule(permission="read", pattern="*", action="allow"),
    Rule(permission="glob", pattern="*", action="allow"),
    Rule(permission="grep", pattern="*", action="allow"),
    Rule(permission="webfetch", pattern="*", action="allow"),
    Rule(permission="websearch", pattern="*", action="allow"),
    Rule(permission="todo", pattern="*", action="allow"),
    Rule(permission="task_status", pattern="*", action="allow"),
    Rule(permission="repo_map", pattern="*", action="allow"),

    # Destructive tools: ask by default
    Rule(permission="write", pattern="*", action="ask"),
    Rule(permission="edit", pattern="*", action="ask"),
    Rule(permission="bash", pattern="*", action="ask"),

    # Task delegation: ask (could spawn implement which writes)
    Rule(permission="task", pattern="*", action="ask"),
]

# ── types ──────────────────────────────────────────────────────────────────

class PermissionRequest(BaseModel):
    id: str
    session_id: str
    tool: str
    patterns: list[str]
    metadata: dict = {}


@dataclass
class PendingEntry:
    info: PermissionRequest
    future: asyncio.Future


class PermissionRejectedError(Exception):
    def __init__(self, tool: str, pattern: str):
        self.tool = tool
        self.pattern = pattern
        super().__init__(f"User rejected {tool} → {pattern}")


# ── service ────────────────────────────────────────────────────────────────

class PermissionService:
    """Checks tool permissions with defaults → session whitelist → ask flow."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingEntry] = {}
        # Session whitelist: tools the user has approved for this session
        self._session_allow: set[str] = set()
        self._session_deny: set[str] = set()

    # ── session whitelist management ─────────────────────────────────────

    def allow(self, tool: str) -> None:
        """Pre-approve a tool for the entire session. No more prompts."""
        self._session_allow.add(tool)
        self._session_deny.discard(tool)
        ui.print(f"[dim]✓ {tool} now allowed for this session[/dim]")

    def deny(self, tool: str) -> None:
        """Block a tool for the entire session."""
        self._session_deny.add(tool)
        self._session_allow.discard(tool)
        ui.print(f"[dim]✗ {tool} now denied for this session[/dim]")

    def show_rules(self) -> str:
        """Format current session rules."""
        lines = ["[bold]Session permissions:[/bold]"]
        lines.append("  [green]Always allowed:[/green] read, glob, grep, webfetch, websearch, todo, task_status")
        if self._session_allow:
            lines.append(f"  [green]Session allow:[/green] {', '.join(sorted(self._session_allow))}")
        if self._session_deny:
            lines.append(f"  [red]Session deny:[/red] {', '.join(sorted(self._session_deny))}")
        lines.append("  [yellow]Ask first:[/yellow] write, edit, bash, task")
        lines.append("")
        lines.append("  Commands: /allow <tool>  /deny <tool>")
        return "\n".join(lines)

    # ── check ────────────────────────────────────────────────────────────

    async def check(
        self,
        tool: str,
        pattern: str = "*",
        session_id: str = "default",
    ) -> Action:
        """Check permission. Evaluates: defaults → session whitelist → ask.

        Returns "allow" (proceed) or "deny" (block silently).
        Raises PermissionRejectedError if user rejects the interactive ask.
        """
        # 1. Session deny overrides everything
        if tool in self._session_deny:
            return "deny"

        # 2. Session allow skips defaults + ask
        if tool in self._session_allow:
            return "allow"

        # 3. Built-in defaults
        rule = evaluate(tool, pattern, DEFAULT_RULES)
        if rule.action == "allow":
            return "allow"
        if rule.action == "deny":
            return "deny"

        # 4. Must ask user
        return await self._ask_user(tool, pattern, session_id)

    async def _ask_user(self, tool: str, pattern: str, session_id: str) -> Action:
        """Interactive ask — simple text input, no raw key artifacts."""
        ui.print("")
        ui.print(f"  [yellow]Allow [bold]{tool}[/bold] → {pattern}?[/yellow]")
        ui.print(f"  [a] Always  [y] Yes once  [n] No")
        try:
            choice = await asyncio.get_event_loop().run_in_executor(
                None, lambda: input("  > ").strip().lower()
            )
        except (EOFError, KeyboardInterrupt):
            return "deny"

        if choice in ("a", "always"):
            self._session_allow.add(tool)
            ui.print(f"[dim]✓ {tool} allowed for this session[/dim]")
            return "allow"
        elif choice in ("y", "yes", ""):
            return "allow"
        else:
            return "deny"

    def list_pending(self) -> list[PermissionRequest]:
        return [entry.info for entry in self._pending.values()]

    def clear(self) -> None:
        for entry in self._pending.values():
            if not entry.future.done():
                entry.future.set_result("deny")
        self._pending.clear()

    def clear_session_permissions(self) -> None:
        """Reset session allow/deny whitelists."""
        self._session_allow.clear()
        self._session_deny.clear()
