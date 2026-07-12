"""Phase 6 restricted shell policy and static access planning."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from voidx.permission.context import PermissionContext
from voidx.permission.grants import resolve_access
from voidx.permission.process_sandbox import default_process_sandbox_capability

Action = Literal["allow", "deny", "defer"]


@dataclass(frozen=True)
class ShellPolicyDecision:
    allowed: bool
    read_only: bool
    reason: str = ""
    access_paths: tuple[Path, ...] = ()


_READ_COMMANDS = {"cat", "head", "tail", "wc", "ls", "pwd", "echo", "printf"}
_POWERSHELL_READ_COMMANDS = {"get-content", "gc", "cat", "type", "get-childitem", "gci", "dir", "ls", "write-output", "echo"}
_DYNAMIC_MARKERS = ("$", "`", "<(", ">(")
_NESTED_INTERPRETERS = {"bash", "sh", "zsh", "fish", "cmd", "powershell", "pwsh", "python", "python3", "node", "ruby", "perl"}
_SHELL_OPERATOR_CHARS = {";", "|", "<", ">", "&", "\n", "\r"}


def shell_policy_for_command(command: str, *, shell: str = "bash") -> ShellPolicyDecision:
    stripped = command.strip()
    if not stripped or stripped.startswith("#"):
        return ShellPolicyDecision(True, True)
    if any(marker in stripped for marker in _DYNAMIC_MARKERS):
        return ShellPolicyDecision(False, False, "shell policy denied dynamic syntax")
    if shell == "powershell" and any(ch in stripped for ch in "()"):
        return ShellPolicyDecision(False, False, "shell policy denied dynamic or compound syntax")
    if _has_shell_operator(stripped, shell=shell):
        return ShellPolicyDecision(False, False, "shell policy denied dynamic or compound syntax")
    try:
        words = shlex.split(stripped, posix=shell == "bash")
    except ValueError:
        return ShellPolicyDecision(False, False, "shell policy denied unparsable command")
    if not words:
        return ShellPolicyDecision(True, True)
    if any(token in {";", "&&", "||", "|", "|&", ">", ">>", "<"} for token in words):
        return ShellPolicyDecision(False, False, "shell policy denied dynamic or compound syntax")
    program = words[0].lower()
    if program in _NESTED_INTERPRETERS:
        return ShellPolicyDecision(False, False, "shell policy denied nested interpreter")
    if shell == "powershell":
        return _powershell_policy(words)
    return _bash_policy(words)




def _has_shell_operator(command: str, *, shell: str = "bash") -> bool:
    in_single = False
    in_double = False
    escaped = False
    for ch in command:
        if escaped:
            escaped = False
            continue
        if shell == "bash" and ch == "\\" and not in_single:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue
        if not in_single and not in_double and ch in _SHELL_OPERATOR_CHARS:
            return True
    return False
def shell_sandbox_precheck(args: dict, context: PermissionContext, *, shell: str = "bash") -> tuple[Action, str | None]:
    if context.sandbox_mode == "danger-full-access":
        return "allow", None
    command = str(args.get("command") or "")
    policy = shell_policy_for_command(command, shell=shell)
    if not policy.allowed:
        if "nested interpreter" in policy.reason:
            return "deny", f"shell policy denied: {policy.reason}"
        return "defer", f"shell policy deferred: {policy.reason}"
    capability = getattr(context, "process_sandbox", None) or default_process_sandbox_capability()
    if not capability.usable_for(shell):
        return "allow", None
    for raw_path in policy.access_paths:
        resolution = resolve_access(
            context.workspace,
            str(raw_path),
            access="write",
            access_grants=context.access_grants,
            require_exists=False,
            allow_missing_write_file=True,
        )
        if resolution.action != "allow":
            return "defer", "shell policy deferred: external path requires writable grant"
    return "allow", None


def _requires_user_approval(reason: str) -> bool:
    return reason in {
        "unknown shell command",
        "unknown powershell command",
        "shell policy denied nested interpreter",
    }


def _bash_policy(words: list[str]) -> ShellPolicyDecision:
    program = words[0].lower()
    if program not in _READ_COMMANDS:
        return ShellPolicyDecision(False, False, "unknown shell command")
    access_paths = tuple(Path(arg) for arg in words[1:] if _looks_like_path(arg))
    return ShellPolicyDecision(True, True, access_paths=access_paths)


def _powershell_policy(words: list[str]) -> ShellPolicyDecision:
    program = words[0].lower()
    if program not in _POWERSHELL_READ_COMMANDS:
        return ShellPolicyDecision(False, False, "unknown powershell command")
    access_paths = tuple(Path(_clean_path_arg(arg)) for arg in words[1:] if not arg.startswith("-") and _looks_like_path(arg))
    return ShellPolicyDecision(True, True, access_paths=access_paths)


def _clean_path_arg(value: str) -> str:
    return value.strip().strip("'").strip('"').strip("`")


def _looks_like_path(value: str) -> bool:
    value = _clean_path_arg(value)
    if not value or value.startswith("-"):
        return False
    return value.startswith(("/", "~", ".")) or "/" in value or "\\" in value or "." in Path(value).name
