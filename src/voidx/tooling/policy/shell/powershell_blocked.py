"""PowerShell command safety checks — blocked patterns, sandbox denial.

Windows-semantic dangerous command interception: Stop-Computer, Format-Volume,
Invoke-Expression with download, Start-Process -Verb RunAs, etc.
"""

from __future__ import annotations

import re

# Patterns that are always blocked regardless of permission
# All patterns use re.IGNORECASE at match time (see _check_command).
_BLOCKED = [
    (r"\bStop-Computer\b", "Stop-Computer is blocked — system shutdown"),
    (r"\bRestart-Computer\b", "Restart-Computer is blocked — system reboot"),
    (r"\bShutdown-Computer\b", "Shutdown-Computer is blocked — system shutdown"),
    (r"\bFormat-Volume\b", "Format-Volume is blocked — filesystem formatting"),
    (r"\bSet-ExecutionPolicy\b.*\b(Unrestricted|Bypass)\b", "Set-ExecutionPolicy Unrestricted/Bypass is blocked"),
    # Invoke-Expression / iex executes arbitrary strings — block unconditionally.
    # The download-specific patterns below are kept for clearer error messages.
    (r"\bInvoke-Expression\b", "Invoke-Expression is blocked — arbitrary code execution"),
    (r"\biex\b", "iex (Invoke-Expression) is blocked — arbitrary code execution"),
    (r"\b(Invoke-WebRequest|Invoke-RestMethod|iwr|irm)\b.*\|\s*(iex|Invoke-Expression)\b", "download piped to iex is blocked"),
    (r"\b(curl|wget)\b.*\|\s*(iex|Invoke-Expression)\b", "curl/wget piped to iex is blocked"),
    (r"\bStart-Process\b.*-Verb\s+RunAs", "Start-Process -Verb RunAs is blocked — privilege escalation"),
    (r"\bNew-Service\b", "New-Service is blocked"),
    (r"\bRemove-Service\b", "Remove-Service is blocked"),
    (r"\bSet-ItemProperty\b.*HKLM:\\", "Set-ItemProperty on HKLM is blocked — registry modification"),
    (r"\bcmd\s+/c\b", "cmd /c is blocked — nested cmd bypass"),
    # Remove-Item -Force on critical paths — order-independent via lookahead:
    # the command must contain Remove-Item AND -Force AND a critical path,
    # in any order (PowerShell allows flexible param order and pipelines
    # can place the path on the left side of |).
    (r"(?=.*\bRemove-Item\b)(?=.*-Force)(?=.*\b(?:C:\\|C:\\Windows|HKLM:\\))", "Remove-Item -Force on critical paths is blocked"),
    # -EncodedCommand hides arbitrary code in base64 — block unconditionally.
    (r"-EncodedCommand\b", "-EncodedCommand is blocked — hidden code execution"),
    # WMI/CIM shutdown paths bypass Stop-Computer.
    (r"\bInvoke-WmiMethod\b.*\bWin32_OperatingSystem\b.*\bWin32Shutdown\b", "Invoke-WmiMethod Win32Shutdown is blocked — system shutdown via WMI"),
    (r"\bInvoke-CimMethod\b.*\bWin32_OperatingSystem\b.*\bWin32Shutdown\b", "Invoke-CimMethod Win32Shutdown is blocked — system shutdown via CIM"),
]


def _normalize_command(command: str) -> str:
    """Strip common PowerShell escapes so blocked patterns can't be bypassed."""
    s = command.strip()
    # Normalize backtick escapes: `c → c
    s = re.sub(r"`(.)", r"\1", s)
    # Normalize variable subexpressions to a placeholder
    s = re.sub(r"\$\([^)]*\)", "SUB", s)
    s = re.sub(r"\$\w+", "SUB", s)
    return s


def _check_command(command: str) -> str | None:
    """Return block reason if command matches a dangerous pattern, else None.

    Scans the raw command first so dangerous cmdlets hidden inside
    subexpressions ($(...)) are caught before normalization replaces them
    with a placeholder. Then scans the normalized form to catch backtick
    and quote-based bypasses.
    """
    for pattern, reason in _BLOCKED:
        if re.search(pattern, command, re.IGNORECASE):
            return f"Blocked: {reason}\n  command: {command.strip()[:120]}"
    normalized = _normalize_command(command)
    if normalized != command:
        for pattern, reason in _BLOCKED:
            if re.search(pattern, normalized, re.IGNORECASE):
                return f"Blocked: {reason}\n  command: {command.strip()[:120]}"
    return None
