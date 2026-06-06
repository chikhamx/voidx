"""Permission service — user-facing permission state and compatibility helpers.

Default rules: read-only tools are auto-allowed, write/bash/agent implement need approval.

Session whitelist: once user says "always", the tool is remembered.
Manage via /allow <tool>, /deny <tool>, /permissions commands.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from voidx.config import ApprovalReviewer, PermissionMode, permission_mode_defaults, permission_mode_reviewer_default
from voidx.permission.engine import (
    BASIC_RULES,
    PermissionContext,
    authorize_tool_call,
    classify_tool_call,
    sandbox_denial_reason,
    tool_call_from_pattern,
)
from voidx.permission.schema import Action

PermissionNotifier = Callable[[str], None]


DEFAULT_RULES = BASIC_RULES

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
    """Checks tool permissions with sandbox → defaults → session whitelist → ask flow."""

    def __init__(
        self,
        permission_mode: str = "default",
        sandbox_mode: str = "workspace-write",
        sandbox_workspace_write: list[str] | None = None,
        approval_policy: str = "untrusted",
        approval_reviewer: str = "user",
        notifier: PermissionNotifier | None = None,
    ) -> None:
        self._pending: dict[str, PendingEntry] = {}
        self._notifier = notifier
        # Session whitelist: tools the user has approved for this session
        self._session_allow: set[str] = set()
        self._session_deny: set[str] = set()
        # Sandbox / approval — persistent filesystem + frequency controls
        try:
            parsed_mode = PermissionMode(permission_mode)
        except ValueError:
            parsed_mode = PermissionMode.CUSTOM
        if (
            parsed_mode == PermissionMode.DEFAULT
            and (
                sandbox_mode != "workspace-write"
                or approval_policy != "untrusted"
                or approval_reviewer != "user"
            )
        ):
            parsed_mode = PermissionMode.CUSTOM
        self.permission_mode = parsed_mode.value
        if parsed_mode == PermissionMode.CUSTOM:
            self.sandbox_mode = sandbox_mode
            self.approval_policy = approval_policy
            self.approval_reviewer = approval_reviewer
        else:
            mode_sandbox, mode_approval = permission_mode_defaults(parsed_mode)
            self.sandbox_mode = mode_sandbox.value
            self.approval_policy = mode_approval.value
            self.approval_reviewer = permission_mode_reviewer_default(parsed_mode).value
        self.sandbox_workspace_write = sandbox_workspace_write or []

    # ── session whitelist management ─────────────────────────────────────

    def allow(self, tool: str) -> None:
        """Pre-approve a tool for the entire session. No more prompts."""
        self._session_allow.add(tool)
        self._session_deny.discard(tool)
        self._notify(f"[dim]✓ {tool} now allowed for this session[/dim]")

    def allow_silent(self, tool: str) -> None:
        """Pre-approve a tool for the session without UI noise."""
        self._session_allow.add(tool)
        self._session_deny.discard(tool)

    def deny(self, tool: str) -> None:
        """Block a tool for the entire session."""
        self._session_deny.add(tool)
        self._session_allow.discard(tool)
        self._notify(f"[dim]✗ {tool} now denied for this session[/dim]")

    def deny_silent(self, tool: str) -> None:
        """Block a tool for the session without UI noise."""
        self._session_deny.add(tool)
        self._session_allow.discard(tool)

    def status_label(self) -> str:
        if not self._session_allow and not self._session_deny:
            return self.permission_mode_label()
        parts: list[str] = [self.permission_mode_label()]
        if self._session_allow:
            parts.append(f"+{len(self._session_allow)}")
        if self._session_deny:
            parts.append(f"-{len(self._session_deny)}")
        return " ".join(parts)

    def set_permission_mode(self, mode: str) -> None:
        try:
            parsed = PermissionMode(mode)
        except ValueError:
            parsed = PermissionMode.CUSTOM
        self.permission_mode = parsed.value
        if parsed == PermissionMode.CUSTOM:
            return
        sandbox_mode, approval_policy = permission_mode_defaults(parsed)
        self.sandbox_mode = sandbox_mode.value
        self.approval_policy = approval_policy.value
        self.approval_reviewer = permission_mode_reviewer_default(parsed).value
        self.sandbox_workspace_write = []

    def mark_custom_mode(self) -> None:
        self.permission_mode = PermissionMode.CUSTOM.value

    def permission_mode_label(self) -> str:
        labels = {
            PermissionMode.DEFAULT.value: "Default",
            PermissionMode.READ_ONLY.value: "Read only",
            PermissionMode.ACCEPT_EDITS.value: "Accept edits",
            PermissionMode.AUTO_REVIEW.value: "Auto review",
            PermissionMode.FULL_ACCESS.value: "Full access",
            PermissionMode.CUSTOM.value: "Custom",
        }
        return labels.get(self.permission_mode, "Custom")

    def status_details(self) -> tuple[str, str, str]:
        """Return (sandbox_label, approval_label, session_label) for UI."""
        return (
            self._sandbox_label(),
            self._approval_label(),
            self._session_short(),
        )

    def _sandbox_label(self) -> str:
        if self.sandbox_mode == "read-only":
            return "r-o"
        if self.sandbox_mode == "workspace-write":
            return "w-write"
        return "danger"

    def _sandbox_short(self) -> str:
        labels = {
            "read-only": "r-o",
            "workspace-write": "w-write",
            "danger-full-access": "danger",
        }
        return labels.get(self.sandbox_mode, self.sandbox_mode)

    def _approval_label(self) -> str:
        labels = {
            "untrusted": "ask",
            "on-failure": "on-fail",
            "on-request": "on-req",
            "never": "auto",
        }
        return labels.get(self.approval_policy, self.approval_policy)

    def _reviewer_label(self) -> str:
        labels = {
            ApprovalReviewer.USER.value: "user",
            ApprovalReviewer.AUTO_REVIEW.value: "reviewer",
        }
        return labels.get(self.approval_reviewer, self.approval_reviewer)

    def _session_short(self) -> str:
        if self._session_allow and not self._session_deny:
            return f"+{len(self._session_allow)}"
        if self._session_deny and not self._session_allow:
            return f"-{len(self._session_deny)}"
        if self._session_allow and self._session_deny:
            return f"+{len(self._session_allow)}/-{len(self._session_deny)}"
        return "default"

    def show_rules(self) -> str:
        """Format current sandbox, approval, and session rules."""
        lines = ["[bold]Session permissions:[/bold]"]
        lines.append(f"  Mode: [cyan]{self.permission_mode_label()}[/cyan] ({self.permission_mode})")
        lines.append(
            f"  Sandbox: [cyan]{self.sandbox_mode}[/cyan]  "
            f"Approval: [cyan]{self.approval_policy}[/cyan]  "
            f"Reviewer: [cyan]{self.approval_reviewer}[/cyan]"
        )
        if self.sandbox_workspace_write:
            lines.append(f"  Extra write paths: [dim]{', '.join(self.sandbox_workspace_write)}[/dim]")
        lines.append("  [green]Always allowed:[/green] read, glob, grep, webfetch, websearch, todo, task_status, repo_map, lsp read tools, read-only agents, read-only bash")
        if self._session_allow:
            lines.append(f"  [green]Session allow:[/green] {', '.join(sorted(self._session_allow))}")
        if self._session_deny:
            lines.append(f"  [red]Session deny:[/red] {', '.join(sorted(self._session_deny))}")
        lines.append("  [yellow]Ask first:[/yellow] write, edit, write-capable bash, lsp_format, agent=implement, mcp__*")
        lines.append("")
        lines.append("  Commands: /permission-mode  /allow <tool>  /deny <tool>  /sandbox [r-o|w-write|danger]  /approval [ask|on-fail|auto]")
        return "\n".join(lines)

    # ── sandbox ──────────────────────────────────────────────────────────

    def check_sandbox(self, tool_name: str, args: dict, workspace: str) -> str | None:
        """Sandbox layer: filesystem boundary enforcement.

        Returns None if the tool call is allowed, or a human-readable
        rejection reason.  Always returns None under danger-full-access.
        """
        if self.sandbox_mode == "danger-full-access":
            return None

        context = self._context(workspace=workspace)
        return sandbox_denial_reason(
            classify_tool_call({"name": tool_name, "args": args}),
            context,
        )

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
        action = self.decide(tool, pattern)
        if action != "ask":
            return action
        return await self._ask_user(tool, pattern, session_id)

    def decide(self, tool: str, pattern: str = "*") -> Action:
        """Return the non-interactive permission decision for a tool call."""
        return authorize_tool_call(tool_call_from_pattern(tool, pattern), self._context()).action

    async def _ask_user(self, tool: str, pattern: str, session_id: str) -> Action:
        """Interactive ask — simple text input, no raw key artifacts."""
        self._notify("")
        self._notify(f"  [yellow]Allow [bold]{tool}[/bold] → {pattern}?[/yellow]")
        self._notify("  [a] Always  [y] Yes once  [n] No")
        try:
            choice = await asyncio.get_event_loop().run_in_executor(
                None, lambda: input("  > ").strip().lower()
            )
        except (EOFError, KeyboardInterrupt):
            return "deny"

        if choice in ("a", "always"):
            self._session_allow.add(tool)
            self._notify(f"[dim]✓ {tool} allowed for this session[/dim]")
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

    def _context(self, *, workspace: str = ".") -> PermissionContext:
        return PermissionContext(
            workspace=workspace,
            permission_mode=self.permission_mode,
            sandbox_mode=self.sandbox_mode,
            sandbox_workspace_write=tuple(self.sandbox_workspace_write),
            approval_policy=self.approval_policy,
            approval_reviewer=self.approval_reviewer,
            session_allow=frozenset(self._session_allow),
            session_deny=frozenset(self._session_deny),
        )

    def _notify(self, message: str) -> None:
        if self._notifier is not None:
            self._notifier(message)
        else:
            print(message)
