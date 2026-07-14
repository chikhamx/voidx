"""Permission service — user-facing permission state and compatibility helpers.

Default rules: read-only tools are auto-allowed, write/bash/agent implement need approval.

Session whitelist: once user says "always", the tool is remembered.
Manage via /allow <tool>, /deny <tool>, /permissions commands.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from pydantic import BaseModel

from voidx.config import PermissionPreset
from voidx.permission.engine import (
    BASIC_RULES,
    PermissionContext,
    authorize_tool_call,
    build_pattern,
    classify_tool_call,
    is_safe_bash,
    sandbox_denial_reason,
    tool_call_from_pattern,
)
from voidx.permission.grants import (
    AccessGrant,
    AccessGrants,
    ApprovalPrecondition,
    GrantDelta,
    GrantUpdateResult,
    PathGrantLockManager,
    delta_for_grant,
)
from voidx.permission.schema import Action
from voidx.permission.sandbox import check_sandbox_bash

PermissionNotifier = Callable[[str], None]


DEFAULT_RULES = BASIC_RULES


def is_safe_bash_command(command: str) -> bool:
    return is_safe_bash(command)


def bash_sandbox_denial(command: str, workspace: str, extra_paths: list[str]) -> str | None:
    return check_sandbox_bash(command, workspace, extra_paths)

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


_EXECUTION_LEASE_TOKEN = object()
_SUBAGENT_SNAPSHOT_TOKEN = object()


class ExecutionLease:
    __slots__ = ("_service", "_token", "_released")

    def __init__(self, service: "PermissionService", token: object) -> None:
        if token is not _EXECUTION_LEASE_TOKEN:
            raise TypeError("ExecutionLease cannot be constructed directly")
        self._service = service
        self._token = token
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._service._release_execution_lease(self)

    async def __aenter__(self) -> "ExecutionLease":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.release()


@dataclass(frozen=True)
class SubagentPermissionSnapshot:
    access_grants: AccessGrants
    revocation_epoch: int
    _token: object
    _current_revocation_epoch: Callable[[], int] | None = None

    @classmethod
    def capture(cls, service: "PermissionService") -> "SubagentPermissionSnapshot":
        return cls(
            service.get_access_grants(),
            service.revocation_epoch,
            _SUBAGENT_SNAPSHOT_TOKEN,
            lambda: service.revocation_epoch,
        )

    @classmethod
    def from_parts(
        cls,
        access_grants: AccessGrants,
        revocation_epoch: int,
        current_revocation_epoch: Callable[[], int] | None = None,
    ) -> "SubagentPermissionSnapshot":
        return cls(access_grants, revocation_epoch, _SUBAGENT_SNAPSHOT_TOKEN, current_revocation_epoch)

    def __post_init__(self) -> None:
        if self._token is not _SUBAGENT_SNAPSHOT_TOKEN:
            raise TypeError("SubagentPermissionSnapshot cannot be constructed directly")

    def get_access_grants(self, *, current_revocation_epoch: int | None = None) -> AccessGrants:
        epoch = current_revocation_epoch
        if epoch is None and self._current_revocation_epoch is not None:
            epoch = self._current_revocation_epoch()
        if epoch != self.revocation_epoch:
            raise PermissionError("Subagent permission snapshot was revoked")
        return self.access_grants

    def add_grant(self, grant: AccessGrant) -> None:
        raise PermissionError("Subagents cannot add grant")


# ── service ────────────────────────────────────────────────────────────────

class PermissionService:
    """Checks tool permissions with sandbox → defaults → session whitelist → ask flow."""

    def __init__(
        self,
        permission_preset: str = PermissionPreset.SAFE.value,
        sandbox_readable_files: list[str] | None = None,
        sandbox_readable_dirs: list[str] | None = None,
        sandbox_writable_files: list[str] | None = None,
        sandbox_writable_dirs: list[str] | None = None,
        persistent_grants: list[AccessGrant] | None = None,
        notifier: PermissionNotifier | None = None,
        permission_state_ready: bool = True,
        persistent_grant_writer: Callable[[GrantDelta], object] | None = None,
    ) -> None:
        self._pending: dict[str, PendingEntry] = {}
        self._notifier = notifier
        self._session_allow: set[str] = set()
        self._session_deny: set[str] = set()
        self._runtime_grants: list[AccessGrant] = []
        self._session_grants: list[AccessGrant] = []
        self._persistent_grants: list[AccessGrant] = list(persistent_grants or [])
        self._grant_lock_manager = PathGrantLockManager()
        self._commit_lock = asyncio.Lock()
        self._active_execution_leases: set[ExecutionLease] = set()
        self.state_revision = 0
        self.permissions_revision = 0
        self.permission_state_ready = permission_state_ready
        self.revocation_epoch = 0
        self._persistent_grant_writer = persistent_grant_writer
        try:
            self.permission_preset = PermissionPreset(permission_preset).value
        except ValueError:
            self.permission_preset = PermissionPreset.SAFE.value
        self.sandbox_readable_files = sandbox_readable_files or []
        self.sandbox_readable_dirs = sandbox_readable_dirs or []
        self.sandbox_writable_files = sandbox_writable_files or []
        self.sandbox_writable_dirs = sandbox_writable_dirs or []
        self.process_sandbox = None

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
        return self.permission_preset_label()

    def set_permission_preset(self, preset: str) -> None:
        if self._active_execution_leases:
            raise PermissionError("Cannot change permission preset while active execution lease exists")
        try:
            parsed = PermissionPreset(preset.replace("-", "_"))
        except ValueError as exc:
            raise PermissionError(f"Invalid permission preset: {preset}") from exc
        if self.permission_preset != parsed.value:
            self.permission_preset = parsed.value
            self.state_revision += 1
            self.revocation_epoch += 1

    @property
    def sandbox_mode(self) -> str:
        try:
            preset = PermissionPreset(self.permission_preset)
            return preset.sandbox_mode
        except ValueError:
            return "workspace-write"

    @property
    def approval_policy(self) -> str:
        try:
            preset = PermissionPreset(self.permission_preset)
            return preset.approval_policy
        except ValueError:
            return "untrusted"

    def get_access_grants(self) -> AccessGrants:
        return AccessGrants.from_parts(
            readable_files=self.sandbox_readable_files,
            readable_dirs=self.sandbox_readable_dirs,
            writable_files=self.sandbox_writable_files,
            writable_dirs=self.sandbox_writable_dirs,
            extra_grants=[*self._runtime_grants, *self._session_grants, *self._persistent_grants],
            permissions_revision=self.permissions_revision,
            state_revision=self.state_revision,
            revocation_epoch=self.revocation_epoch,
            permission_state_ready=self.permission_state_ready,
            permission_preset=self.permission_preset,
        )

    async def add_grant(
        self,
        grant: AccessGrant,
        *,
        precondition: ApprovalPrecondition | None = None,
    ) -> GrantUpdateResult:
        async with self._commit_lock:
            if precondition is not None and (
                precondition.permission_preset != self.permission_preset
                or precondition.revocation_epoch != self.revocation_epoch
            ):
                return GrantUpdateResult(
                    persistent=grant.persistence == "persistent",
                    ok=False,
                    committed=False,
                    durable=None,
                    applied=False,
                    conflict=True,
                    restart_required=False,
                    state_revision=self.state_revision,
                    permissions_revision=self.permissions_revision,
                    error="Approval precondition no longer matches current permission state",
                )
            if grant.persistence == "persistent" and self._persistent_grant_writer is not None:
                self._persistent_grant_writer(delta_for_grant(grant))
            target = self._persistent_grants if grant.persistence == "persistent" else self._runtime_grants if grant.persistence == "runtime" else self._session_grants
            applied = False
            if grant not in target:
                target.append(grant)
                self.state_revision += 1
                applied = True
                if grant.persistence == "persistent":
                    self.permissions_revision += 1
            return GrantUpdateResult(
                persistent=grant.persistence == "persistent",
                ok=True,
                committed=True,
                durable=True if grant.persistence == "persistent" else None,
                applied=applied,
                conflict=False,
                restart_required=False,
                state_revision=self.state_revision,
                permissions_revision=self.permissions_revision,
            )
    async def acquire_grant_targets(self, paths, *, final_paths=None) -> object:
        if final_paths is not None:
            return await self._grant_lock_manager.acquire_final_targets(paths, final_paths)
        return await self._grant_lock_manager.acquire_request_targets(paths)

    async def acquire_execution_lease(self) -> ExecutionLease:
        lease = ExecutionLease(self, _EXECUTION_LEASE_TOKEN)
        self._active_execution_leases.add(lease)
        return lease

    @asynccontextmanager
    async def execution_lease_for_tool(self, tool_name: str) -> AsyncIterator[ExecutionLease]:
        lease = await self.acquire_execution_lease()
        try:
            yield lease
        finally:
            await lease.release()

    def has_active_execution_lease(self, lease: ExecutionLease) -> bool:
        return lease in self._active_execution_leases and not lease._released

    def _release_execution_lease(self, lease: ExecutionLease) -> None:
        if lease in self._active_execution_leases:
            self._active_execution_leases.remove(lease)

    def permission_preset_label(self) -> str:
        labels = {
            PermissionPreset.READ_ONLY.value: "Read only",
            PermissionPreset.SAFE.value: "Safe",
            PermissionPreset.PROJECT_TRUSTED.value: "Project trusted",
            PermissionPreset.FULL_ACCESS.value: "Full access",
        }
        return labels.get(self.permission_preset, "Safe")

    def status_details(self) -> tuple[str, str, str]:
        """Return (sandbox_label, approval_label, session_label) for UI."""
        return (
            self._sandbox_label(),
            self._approval_label(),
            self._session_short(),
        )

    def _sandbox_label(self) -> str:
        return self.permission_preset

    def _sandbox_short(self) -> str:
        return self.permission_preset

    def _approval_label(self) -> str:
        return ""


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
        lines.append(f"  Preset: [cyan]{self.permission_preset_label()}[/cyan] ({self.permission_preset})")
        lines.append(
            f"  Sandbox: [cyan]{self.sandbox_mode}[/cyan]  "
            f"Approval: [cyan]{self.approval_policy}[/cyan]"
        )
        for label, paths in (
            ("Readable files", self.sandbox_readable_files),
            ("Readable dirs", self.sandbox_readable_dirs),
            ("Writable files", self.sandbox_writable_files),
            ("Writable dirs", self.sandbox_writable_dirs),
        ):
            if paths:
                lines.append(f"  {label}: [dim]{', '.join(paths)}[/dim]")
        lines.append("  [green]Always allowed:[/green] read, glob, grep, webfetch, websearch, todo, task_status, lsp, read-only agents, read-only bash")
        if self._session_allow:
            lines.append(f"  [green]Session allow:[/green] {', '.join(sorted(self._session_allow))}")
        if self._session_deny:
            lines.append(f"  [red]Session deny:[/red] {', '.join(sorted(self._session_deny))}")
        lines.append("  [yellow]Ask first:[/yellow] file, line, replace, edit, write-capable bash, agent=implement, mcp__*")
        lines.append("")
        lines.append("  Commands: /permission-preset  /allow <tool>  /deny <tool>")
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
        if self._active_execution_leases:
            raise PermissionError("Cannot clear permissions while active execution lease exists")
        had_permissions = bool(self._session_allow or self._session_deny or self._session_grants)
        self._session_allow.clear()
        self._session_deny.clear()
        self._session_grants.clear()
        if had_permissions:
            self.state_revision += 1
            self.revocation_epoch += 1

    def _context(self, *, workspace: str = ".") -> PermissionContext:
        return PermissionContext(
            workspace=workspace,
            permission_preset=self.permission_preset,
            sandbox_readable_files=tuple(self.sandbox_readable_files),
            sandbox_readable_dirs=tuple(self.sandbox_readable_dirs),
            sandbox_writable_files=tuple(self.sandbox_writable_files),
            sandbox_writable_dirs=tuple(self.sandbox_writable_dirs),
            access_grants=self.get_access_grants(),
            permission_state_ready=self.permission_state_ready,
            session_allow=frozenset(self._session_allow),
            session_deny=frozenset(self._session_deny),
            process_sandbox=self.process_sandbox,
        )

    def _notify(self, message: str) -> None:
        if self._notifier is not None:
            self._notifier(message)
        else:
            print(message)
