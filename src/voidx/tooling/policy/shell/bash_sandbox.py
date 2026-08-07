"""Bash sandbox decision policy over structured execution values."""

from voidx.tooling.policy.filesystem.sandbox import check_sandbox_bash
from voidx.tooling.policy.permission.rules import is_safe_bash


def sandbox_denial_reason(
    command: str,
    *,
    sandbox_mode: str,
    workspace: str,
    write_paths: list[str],
) -> str | None:
    if sandbox_mode == "danger-full-access":
        return None
    if sandbox_mode == "read-only":
        if is_safe_bash(command):
            return None
        return f"SANDBOX READ-ONLY: 'bash' is not allowed.\n  command: {command.strip()[:120]}"
    return check_sandbox_bash(command, workspace, write_paths)
