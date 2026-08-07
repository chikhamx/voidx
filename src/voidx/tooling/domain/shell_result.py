"""Shell route hints and deterministic result factories."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from voidx.tooling.domain.result import ToolResult, tool_timeout_metadata

_HintableTool = Literal["read", "git", "manage", "write", "replace", "find", "search"]


@dataclass
class RouteHint:
    tool_id: _HintableTool
    ui_label: str
    llm_hint: str
    tool_args: dict | None = None


_STATIC_POLICY_HINT = (
    "This shell pattern is statically disallowed in this environment; rephrasing will not help. "
    "Use the dedicated tools instead (read/write/search/manage for files), "
    "or run code through the project's documented entry point."
)


def build_blocked_result(command: str, reason: str) -> ToolResult:
    stderr = f"{reason}\n{_STATIC_POLICY_HINT}"
    payload = {"ok": False, "exit_code": -1, "stdout": "", "stderr": stderr, "blocked": True}
    return ToolResult(
        output=json.dumps(payload, ensure_ascii=False, indent=2),
        display=reason,
        metadata={"command": command, "blocked": True, "error": True},
    )


def build_sandbox_result(command: str, reason: str) -> ToolResult:
    return build_blocked_result(command, reason)


def resolve_shell_workspace(command: str, workspace: str) -> tuple[str, ToolResult | None]:
    raw = str(workspace or "").strip()
    if not raw:
        message = "Shell workspace is not set; cannot choose a working directory."
        return "", ToolResult(
            output=message,
            display=message,
            metadata={"command": command, "error": True, "error_kind": "invalid_workspace"},
        )
    try:
        resolved = Path(raw).expanduser().resolve()
    except (OSError, ValueError) as exc:
        message = f"Shell workspace is invalid: {raw} ({exc})"
        return "", ToolResult(
            output=message,
            display=message,
            metadata={"command": command, "workspace": raw, "error": True, "error_kind": "invalid_workspace"},
        )
    if not resolved.is_dir():
        message = f"Shell workspace does not exist or is not a directory: {resolved}"
        return "", ToolResult(
            output=message,
            display=message,
            metadata={"command": command, "workspace": str(resolved), "error": True, "error_kind": "invalid_workspace"},
        )
    return str(resolved), None


def build_hint_result(command: str, hint: RouteHint, tool_label: str) -> ToolResult:
    return ToolResult(
        title=f"{tool_label} route hint: {command}",
        output=f"[{hint.ui_label}]\nCommand not executed because a specialized tool is available.",
        summary="route hint",
        metadata={"command": command, "skipped": True, "route_hint": {"tool_id": hint.tool_id, "command": command}},
        next_step_hint=hint.llm_hint,
    )


def build_timeout_result(command: str, timeout: int) -> ToolResult:
    payload = {"ok": False, "exit_code": -1, "stdout": "", "stderr": "", "timeout": True}
    return ToolResult(
        output=json.dumps(payload, ensure_ascii=False, indent=2),
        display=f"Command timed out after {timeout}s: {command}",
        metadata=tool_timeout_metadata("shell", command=command, exit_code=-1),
    )


def build_success_result(command: str, stdout: str, stderr: str, exit_code: int, tool_label: str) -> ToolResult:
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
    payload = {"ok": exit_code == 0, "exit_code": exit_code, "stdout": stdout, "stderr": stderr}
    return ToolResult(
        title=f"{tool_label}: {command}",
        output=json.dumps(payload, ensure_ascii=False, indent=2),
        display="\n".join(display_parts) or "(no output)",
        summary="" if exit_code == 0 else f"exit {exit_code}",
        metadata={"command": command, "exit_code": exit_code, "ok": exit_code == 0, **({"error": True} if exit_code != 0 else {})},
    )
