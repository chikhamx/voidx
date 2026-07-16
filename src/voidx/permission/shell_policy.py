"""Phase 6 restricted shell policy and static access planning."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from voidx.permission.context import PermissionContext
from voidx.permission.grants import resolve_access
from voidx.permission.risk import RiskAssessment, RiskLevel, RiskTag
from voidx.permission.schema import Action
from voidx.permission.process_sandbox import default_process_sandbox_capability
from voidx.permission.constants import (
    DYNAMIC_MARKERS,
    NESTED_INTERPRETERS,
    POWERSHELL_READ_COMMANDS,
    READ_COMMANDS,
    SHELL_OPERATOR_CHARS,
)


@dataclass(frozen=True)
class ShellPolicyDecision:
    allowed: bool
    read_only: bool
    reason: str = ""
    access_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class HardBlockedShellCommand:
    reason: str
    tags: tuple[RiskTag, ...]


_HARD_BLOCKED_SHELL_PATTERNS: tuple[tuple[str, str, tuple[RiskTag, ...]], ...] = (
    (r"\bsudo\b", "sudo is blocked — privilege escalation", (RiskTag.PRIVILEGE_ESCALATION,)),
    (r"\bchmod\s+.*[0]*7\d{2}\b", "chmod 7xx is blocked — world-writable permissions", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r"\bchmod\s+[0]*\d*7\b", "chmod with 7 is blocked", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r"\bchown\b", "chown is blocked", (RiskTag.PRIVILEGE_ESCALATION,)),
    (r"\bchgrp\b", "chgrp is blocked", (RiskTag.PRIVILEGE_ESCALATION,)),
    (r"\bmkfs\b", "mkfs is blocked — filesystem formatting", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r"\bdd\s+if=.*of=/dev/", "dd to /dev is blocked — raw disk write", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r">\s*/dev/sd", "write to /dev/sd* is blocked", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r"\breboot\b", "reboot is blocked", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r"\bshutdown\b", "shutdown is blocked", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r"\bpoweroff\b", "poweroff is blocked", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r"\binit\s+[06]\b", "init runlevel change is blocked", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r":\(\)\s*\{", "fork bomb pattern is blocked", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r"\bgit\s+push\s+.*(-f|--force).*(main|master)\b", "force push to main/master is blocked", (RiskTag.GIT_PUSH,)),
    (r"\bcurl\b.*\|\s*(bash|sh|/bin/bash|/bin/sh)\b", "curl piped to shell is blocked", (RiskTag.NETWORK, RiskTag.DYNAMIC_SHELL)),
    (r"\bwget\b.*\|\s*(bash|sh|/bin/bash|/bin/sh)\b", "wget piped to shell is blocked", (RiskTag.NETWORK, RiskTag.DYNAMIC_SHELL)),
)


def hard_blocked_shell_command(command: str) -> HardBlockedShellCommand | None:
    normalized = _normalize_shell_command(command)
    for pattern, reason, tags in _HARD_BLOCKED_SHELL_PATTERNS:
        if re.search(pattern, normalized):
            return HardBlockedShellCommand(
                reason=f"Blocked: {reason}\n  command: {command.strip()[:120]}",
                tags=tags,
            )
    return None


def _normalize_shell_command(command: str) -> str:
    s = command.strip()
    s = re.sub(r"\\\s*\n", " ", s)
    s = re.sub(r"\\(.)", r"\1", s)
    s = re.sub(r"\$\([^)]*\)", "SUB", s)
    s = re.sub(r"`[^`]*`", "SUB", s)
    s = re.sub(r"''", "", s)
    return s




def shell_policy_for_command(command: str, *, shell: str = "bash") -> ShellPolicyDecision:
    risk = classify_shell_risk(command, shell=shell)
    if risk.level in {RiskLevel.EXTREME, RiskLevel.BLOCKED}:
        return ShellPolicyDecision(False, False, risk.reason)
    stripped = command.strip()
    if not stripped or stripped.startswith("#"):
        return ShellPolicyDecision(True, True)
    try:
        words = shlex.split(stripped, posix=shell == "bash")
    except ValueError:
        return ShellPolicyDecision(False, False, "shell policy denied unparsable command")
    if not words:
        return ShellPolicyDecision(True, True)
    if shell == "powershell":
        return _powershell_policy(words)
    return _bash_policy(words)


def classify_shell_risk(command: str, *, shell: str = "bash") -> RiskAssessment:
    stripped = command.strip()
    tool_name = "powershell" if shell == "powershell" else "bash"
    if not stripped or stripped.startswith("#"):
        return RiskAssessment.normal(tool_name=tool_name, pattern=stripped, tags=(RiskTag.SAFE_READ,), reason="empty or comment-only shell command")
    if _is_catastrophic_shell_command(stripped):
        return RiskAssessment.blocked(
            tool_name=tool_name,
            pattern=stripped,
            tags=(RiskTag.SYSTEM_DESTRUCTIVE,),
            reason="shell policy blocked catastrophic system command",
        )
    blocked = _blocked_shell_risk(stripped, tool_name=tool_name)
    if blocked is not None:
        return blocked
    hard_blocked = hard_blocked_shell_command(stripped)
    if hard_blocked is not None:
        return RiskAssessment.blocked(
            tool_name=tool_name,
            pattern=stripped,
            tags=hard_blocked.tags,
            reason=hard_blocked.reason,
        )
    tags: list[RiskTag] = []
    reasons: list[str] = []
    if any(marker in stripped for marker in DYNAMIC_MARKERS):
        tags.append(RiskTag.DYNAMIC_SHELL)
        reasons.append("dynamic shell syntax")
    if shell == "powershell" and any(ch in stripped for ch in "()"):
        tags.append(RiskTag.DYNAMIC_SHELL)
        reasons.append("dynamic or compound PowerShell syntax")
    if _has_shell_operator(stripped, shell=shell):
        tags.append(RiskTag.DYNAMIC_SHELL)
        reasons.append("compound shell syntax")
    try:
        words = shlex.split(stripped, posix=shell == "bash")
    except ValueError:
        return RiskAssessment.extreme(
            tool_name=tool_name,
            pattern=stripped,
            tags=(RiskTag.DYNAMIC_SHELL,),
            reason="unparsable shell command",
        )
    if any(token in {";", "&&", "||", "|", "|&", ">", ">>", "<"} for token in words):
        tags.append(RiskTag.DYNAMIC_SHELL)
        reasons.append("compound shell operator")
    program = words[0].lower() if words else ""
    if program in NESTED_INTERPRETERS:
        tags.append(RiskTag.NESTED_INTERPRETER)
        reasons.append("nested interpreter")
    if _is_dependency_install(words):
        tags.append(RiskTag.DEPENDENCY_INSTALL)
        reasons.append("dependency install command")
    if _is_network_command(words):
        tags.append(RiskTag.NETWORK)
        reasons.append("network command")
    if tags:
        deduped = tuple(dict.fromkeys(tags))
        return RiskAssessment.extreme(tool_name=tool_name, pattern=stripped, tags=deduped, reason="shell policy deferred: " + ", ".join(dict.fromkeys(reasons)))
    policy = _powershell_policy(words) if shell == "powershell" else _bash_policy(words)
    if policy.allowed:
        return RiskAssessment.normal(tool_name=tool_name, pattern=stripped, tags=(RiskTag.SAFE_READ,), reason="read-only shell command")
    return RiskAssessment.dangerous(tool_name=tool_name, pattern=stripped, tags=(RiskTag.WORKSPACE_EDIT,), reason=policy.reason)


def _blocked_shell_risk(command: str, *, tool_name: str) -> RiskAssessment | None:
    try:
        words = shlex.split(command, posix=tool_name == "bash")
    except ValueError:
        return None
    program = words[0].lower() if words else ""
    if program == "sudo":
        return RiskAssessment.blocked(
            tool_name=tool_name,
            pattern=command,
            tags=(RiskTag.PRIVILEGE_ESCALATION,),
            reason="Blocked: sudo is blocked — privilege escalation",
        )
    if program in {"reboot", "shutdown", "poweroff"}:
        return RiskAssessment.blocked(
            tool_name=tool_name,
            pattern=command,
            tags=(RiskTag.SYSTEM_DESTRUCTIVE,),
            reason=f"Blocked: {program} is blocked — system destructive command",
        )
    return None



def _has_shell_operator(command: str, *, shell: str) -> bool:
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
        if not in_single and not in_double and ch in SHELL_OPERATOR_CHARS:
            return True
    return False


def shell_sandbox_precheck(args: dict, context: PermissionContext, *, shell: str = "bash") -> tuple[Action, str | None]:
    if context.sandbox_mode == "danger-full-access":
        return "allow", None
    command = str(args.get("command") or "")
    risk = classify_shell_risk(command, shell=shell)
    if risk.level == RiskLevel.BLOCKED:
        return "deny", risk.reason
    policy = shell_policy_for_command(command, shell=shell)
    if not policy.allowed:
        return "defer", policy.reason
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
    if program not in READ_COMMANDS:
        return ShellPolicyDecision(False, False, "unknown shell command")
    access_paths = tuple(Path(arg) for arg in words[1:] if _looks_like_path(arg))
    return ShellPolicyDecision(True, True, access_paths=access_paths)


def _powershell_policy(words: list[str]) -> ShellPolicyDecision:
    program = words[0].lower()
    if program not in POWERSHELL_READ_COMMANDS:
        return ShellPolicyDecision(False, False, "unknown powershell command")
    access_paths = tuple(Path(_clean_path_arg(arg)) for arg in words[1:] if not arg.startswith("-") and _looks_like_path(arg))
    return ShellPolicyDecision(True, True, access_paths=access_paths)


def _is_catastrophic_shell_command(command: str) -> bool:
    lowered = command.lower().strip()
    compact = " ".join(lowered.split())
    if "rm -rf /" in compact or "rm -fr /" in compact:
        targets = {"/", "/home", "~", "$home", "${home}"}
        parts = compact.replace(";", " ").split()
        if any(part.rstrip("/") in targets or part in targets for part in parts[2:]):
            return True
    if compact.startswith(("mkfs", "shutdown", "reboot", "poweroff")):
        return True
    if ":(){ :|:& };:" in compact or ":(){:|:&};:" in compact:
        return True
    if compact.startswith("dd ") and (" of=/dev/" in compact or " of=/dev/disk" in compact):
        return True
    return False


def _is_dependency_install(words: list[str]) -> bool:
    if not words:
        return False
    program = words[0].lower()
    if program in {"pip", "pip3", "npm", "pnpm", "yarn", "brew", "apt", "apt-get"}:
        return any(word.lower() in {"install", "add"} for word in words[1:])
    return False


def _is_network_command(words: list[str]) -> bool:
    return bool(words) and words[0].lower() in {"curl", "wget", "scp", "ssh"}


def _clean_path_arg(value: str) -> str:
    return value.strip().strip("'").strip('"').strip("`")


def _looks_like_path(value: str) -> bool:
    value = _clean_path_arg(value)
    if not value or value.startswith("-"):
        return False
    return value.startswith(("/", "~", ".")) or "/" in value or "\\" in value or "." in Path(value).name
