"""Bash command safety checks — blocked patterns, sandbox denial, process termination."""

from __future__ import annotations

from voidx.permission.service import bash_sandbox_denial, is_safe_bash_command
from voidx.permission.shell_policy import hard_blocked_shell_command
from voidx.tools.base import ToolContext
from voidx.tools.shell.common import terminate_process as _terminate_process


def _check_command(command: str) -> str | None:
    """Return block reason if command matches a dangerous pattern, else None."""
    blocked = hard_blocked_shell_command(command)
    return blocked.reason if blocked is not None else None


def _sandbox_denial(command: str, ctx: ToolContext) -> str | None:
    if ctx.sandbox_mode == "danger-full-access":
        return None
    if ctx.sandbox_mode == "read-only":
        if is_safe_bash_command(command):
            return None
        return f"SANDBOX READ-ONLY: 'bash' is not allowed.\n  command: {command.strip()[:120]}"
    return bash_sandbox_denial(command, ctx.workspace, [*ctx.sandbox_writable_files, *ctx.sandbox_writable_dirs])
