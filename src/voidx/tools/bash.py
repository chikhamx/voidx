"""Bash tool — execute shell commands, capture output. Exact, measurable."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
from contextlib import suppress

from pydantic import BaseModel, Field

from voidx.permission.service import bash_sandbox_denial, is_safe_bash_command
from voidx.tools.base import BaseTool, model_to_json_schema, ToolContext, ToolResult

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


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
        return
    except asyncio.TimeoutError:
        pass

    with suppress(ProcessLookupError):
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=2)


class BashInput(BaseModel):
    command: str = Field(description="Shell command to execute")
    timeout: int = Field(default=120, description="Timeout in seconds")


class BashTool(BaseTool):
    id = "bash"
    description = "Execute a shell command in the workspace directory. Returns stdout, stderr, and exit code. No comments inside the command."

    def parameters_schema(self) -> dict:
        return model_to_json_schema(BashInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = BashInput.model_validate(args)

        blocked = _check_command(inp.command)
        if blocked:
            return ToolResult(output=blocked, metadata={"command": inp.command, "blocked": True})

        blocked = _sandbox_denial(inp.command, ctx)
        if blocked:
            return ToolResult(output=blocked, metadata={"command": inp.command, "blocked": True})

        try:
            proc = await asyncio.create_subprocess_shell(
                inp.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=ctx.workspace,
                start_new_session=hasattr(os, "killpg"),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=inp.timeout
            )
        except asyncio.TimeoutError:
            await _terminate_process(proc)
            return ToolResult(
                output=f"Command timed out after {inp.timeout}s: {inp.command}",
                metadata={"command": inp.command, "exit_code": -1, "timeout": True},
            )

        output_parts = []
        if stdout:
            output_parts.append(stdout.decode("utf-8", errors="replace"))
        if stderr:
            output_parts.append(f"[stderr]\n{stderr.decode('utf-8', errors='replace')}")

        exit_code = proc.returncode or 0
        if exit_code != 0 and not stdout and not stderr:
            output_parts.append(
                "Interactive commands that read from stdin are not supported. "
                "Use non-interactive flags or pipe input via echo/heredoc."
            )

        return ToolResult(
            title=f"Bash: {inp.command}",
            output="\n".join(output_parts) or "(no output)",
            metadata={
                "command": inp.command,
                "exit_code": exit_code,
                "stdout_size": len(stdout) if stdout else 0,
                "stderr_size": len(stderr) if stderr else 0,
            },
        )
