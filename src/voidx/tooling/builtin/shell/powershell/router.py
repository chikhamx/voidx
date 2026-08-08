"""PowerShell command route hint dispatch — suggest specialized tools over raw powershell."""

from __future__ import annotations

from voidx.tooling.builtin.shell.powershell.core import (
    _has_shell_expansion,
    shell_words,
    _strip_cd_prefix,
)
from voidx.tooling.builtin.shell.powershell.hint.file import hint_get_content, hint_out_file
from voidx.tooling.builtin.shell.powershell.hint.search import hint_get_child_item, hint_select_string
from voidx.tooling.builtin.shell.common import RouteHint
from voidx.tooling.builtin.shell.hint.git import hint_git


def try_hint(command: str) -> RouteHint | None:
    """Try to generate a specialized-tool hint for a PowerShell command.

    Catches all exceptions and returns None — hint logic must never break powershell.
    """
    try:
        return _try_hint_impl(command)
    except Exception:
        return None


# Alias mapping: PowerShell built-in aliases → cmdlet
_ALIAS_MAP = {
    "cat": "Get-Content",
    "type": "Get-Content",
    "gc": "Get-Content",
    "dir": "Get-ChildItem",
    "ls": "Get-ChildItem",
    "gci": "Get-ChildItem",
    "sls": "Select-String",
    "echo": "Write-Output",
    "write": "Write-Output",
    "del": "Remove-Item",
    "erase": "Remove-Item",
    "ri": "Remove-Item",
    "rm": "Remove-Item",
}


def _try_hint_impl(command: str) -> RouteHint | None:
    stripped = command.strip()
    if not stripped:
        return None

    stripped = _strip_cd_prefix(stripped)

    # Skip complex commands with subexpressions
    if _has_shell_expansion(stripped):
        return None

    words = shell_words(stripped)
    if not words:
        return None

    # Skip if there's a pipeline (except for Out-File at the end)
    has_pipe = "|" in words
    prog = words[0].lower()

    # Resolve alias to cmdlet
    prog_resolved = _ALIAS_MAP.get(prog, prog).lower()

    if has_pipe:
        pipe_idx = words.index("|")
        if pipe_idx + 1 < len(words):
            after_pipe = words[pipe_idx + 1].lower()
            after_pipe_resolved = _ALIAS_MAP.get(after_pipe, after_pipe).lower()
            if after_pipe_resolved in ("out-file", "set-content", "add-content"):
                return hint_out_file(words[pipe_idx + 1:])
        return None

    # git — reuse shell.hint.git
    if prog == "git" and len(words) >= 2:
        return hint_git(stripped, words)

    # Get-Content / cat / type → read
    if prog_resolved == "get-content":
        return hint_get_content(words)

    # Select-String / sls → search
    if prog_resolved == "select-string":
        return hint_select_string(words)

    # Get-ChildItem / dir / ls / gci → find
    if prog_resolved == "get-childitem":
        return hint_get_child_item(words)

    # Out-File / Set-Content / Add-Content → write (can be piped)
    if prog_resolved in ("out-file", "set-content", "add-content"):
        return hint_out_file(words)

    return None
