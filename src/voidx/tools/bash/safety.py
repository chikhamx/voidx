"""Bash command safety checks — blocked patterns, sandbox denial, process termination."""

from __future__ import annotations

import re

from voidx.permission.service import bash_sandbox_denial, is_safe_bash_command
from voidx.tools.base import ToolContext
from voidx.tools.shell.common import terminate_process as _terminate_process

# Patterns that are always blocked regardless of permission
_BLOCKED = [
    (r"\bsudo\b", "sudo is blocked — privilege escalation"),
    (r"\bchmod\s+.*[0]*7\d{2}\b", "chmod 7xx is blocked — world-writable permissions"),
    (r"\bchmod\s+[0]*\d*7\b", "chmod with 7 is blocked"),
    (r"\bchown\b", "chown is blocked"),
    (r"\bchgrp\b", "chgrp is blocked"),
    (r"\bmkfs\b", "mkfs is blocked — filesystem formatting"),
    (r"\bdd\s+if=.*of=/dev/", "dd to /dev is blocked — raw disk write"),
    (r">\s*/dev/sd", "write to /dev/sd* is blocked"),
    (r"\breboot\b", "reboot is blocked"),
    (r"\bshutdown\b", "shutdown is blocked"),
    (r"\bpoweroff\b", "poweroff is blocked"),
    (r"\binit\s+[06]\b", "init runlevel change is blocked"),
    (r":\(\)\s*\{", "fork bomb pattern is blocked"),
    (r"\bgit\s+push\s+.*(-f|--force).*(main|master)\b", "force push to main/master is blocked"),
    (r"\bcurl\b.*\|\s*(bash|sh|/bin/bash|/bin/sh)\b", "curl piped to shell is blocked"),
    (r"\bwget\b.*\|\s*(bash|sh|/bin/bash|/bin/sh)\b", "wget piped to shell is blocked"),
]


def _normalize_command(command: str) -> str:
    """Strip common shell escapes so blocked patterns can't be bypassed."""
    s = command.strip()
    s = re.sub(r"\\\s*\n", " ", s)
    s = re.sub(r"\\(.)", r"\1", s)
    s = re.sub(r"\$\([^)]*\)", "SUB", s)
    s = re.sub(r"`[^`]*`", "SUB", s)
    s = re.sub(r"''", "", s)
    return s


def _check_command(command: str) -> str | None:
    """Return block reason if command matches a dangerous pattern, else None."""
    normalized = _normalize_command(command)
    for pattern, reason in _BLOCKED:
        if re.search(pattern, normalized):
            return f"Blocked: {reason}\n  command: {command.strip()[:120]}"
    return None


def _sandbox_denial(command: str, ctx: ToolContext) -> str | None:
    if ctx.sandbox_mode == "danger-full-access":
        return None
    if ctx.sandbox_mode == "read-only":
        if is_safe_bash_command(command):
            return None
        return f"SANDBOX READ-ONLY: 'bash' is not allowed.\n  command: {command.strip()[:120]}"
    return bash_sandbox_denial(command, ctx.workspace, ctx.sandbox_extra_paths)
