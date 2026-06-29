"""Platform-agnostic shell tool primitives — RouteHint, result factories, process termination.

Shared by bash (unix) and powershell (Windows) tools to avoid duplication.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal

from voidx.tools.base import ToolResult

_HintableTool = Literal["read", "git", "file", "write", "replace", "glob", "grep"]


@dataclass
class RouteHint:
    tool_id: _HintableTool
    ui_label: str
    llm_hint: str


# ── result factory functions ────────────────────────────────────────────────


def build_blocked_result(command: str, reason: str) -> ToolResult:
    """Build a ToolResult for a blocked command (dangerous pattern or sandbox denial)."""
    payload = {"ok": False, "exit_code": -1, "stdout": "", "stderr": reason, "blocked": True}
    return ToolResult(
        output=json.dumps(payload, ensure_ascii=False),
        display=reason,
        metadata={"command": command, "blocked": True, "error": True},
    )


def build_sandbox_result(command: str, reason: str) -> ToolResult:
    """Build a ToolResult for a sandbox denial (same structure as blocked)."""
    return build_blocked_result(command, reason)


def build_hint_result(command: str, hint: RouteHint, tool_label: str) -> ToolResult:
    """Build a ToolResult for a route hint (command not executed, specialized tool suggested)."""
    return ToolResult(
        title=f"{tool_label} route hint: {command}",
        output=(
            f"[{hint.ui_label}]\n"
            "Command not executed because a specialized tool is available."
        ),
        summary="route hint",
        metadata={
            "command": command,
            "skipped": True,
            "route_hint": {"tool_id": hint.tool_id, "command": command},
        },
        next_step_hint=hint.llm_hint,
    )


def build_timeout_result(command: str, timeout: int) -> ToolResult:
    """Build a ToolResult for a command that timed out."""
    payload = {"ok": False, "exit_code": -1, "stdout": "", "stderr": "", "timeout": True}
    display = f"Command timed out after {timeout}s: {command}"
    return ToolResult(
        output=json.dumps(payload, ensure_ascii=False),
        display=display,
        metadata={"command": command, "exit_code": -1, "timeout": True, "error": True},
    )


def build_success_result(
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int,
    tool_label: str,
) -> ToolResult:
    """Build a ToolResult for a completed command (success or non-zero exit)."""
    display_parts = []
    if stdout:
        display_parts.append(stdout)
    if stderr:
        display_parts.append(f"[stderr]\n{stderr}")
    if exit_code != 0 and not stdout and not stderr:
        display_parts.append(
            "Interactive commands that read from stdin are not supported. "
            "Use non-interactive flags or pipe input."
        )

    payload = {
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }

    return ToolResult(
        title=f"{tool_label}: {command}",
        output=json.dumps(payload, ensure_ascii=False),
        display="\n".join(display_parts) or "(no output)",
        summary=f"exit {exit_code}",
        metadata={
            "command": command,
            "exit_code": exit_code,
            "ok": exit_code == 0,
            **({"error": True} if exit_code != 0 else {}),
        },
    )


# ── process termination ─────────────────────────────────────────────────────


async def terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Terminate a subprocess, escalating from SIGTERM/terminate to SIGKILL/kill.

    On Unix, kills the entire process group (os.killpg).
    On Windows, falls back to proc.terminate() / proc.kill().
    """
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
