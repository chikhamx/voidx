"""PowerShell syntax parsing primitives — not shlex, not copied from bash.

Handles PowerShell-specific syntax: -Param, | pipeline objects, '...'/"..." quotes,
$var/$(...)/@(...) expansion, Set-Location; cmd (no && in PS 5.1).
"""

from __future__ import annotations

import re

_RE_CD_PREFIX = re.compile(r"^Set-Location\s+\S+\s*;\s*", re.IGNORECASE)
_RE_SEMI = re.compile(r";")


def shell_words(command: str) -> list[str]:
    """Split a PowerShell command into tokens.

    Handles single/double quotes and pipe separators. Does NOT use shlex —
    PowerShell's -Param syntax and | pipeline are not posix shell.
    """
    words: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        ch = command[i]
        if in_single:
            if ch == "'":
                # Check for escaped quote ''
                if i + 1 < len(command) and command[i + 1] == "'":
                    current.append("'")
                    i += 2
                    continue
                in_single = False
            else:
                current.append(ch)
        elif in_double:
            if ch == '"':
                in_double = False
            else:
                current.append(ch)
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch.isspace() or ch == "|":
                if current:
                    words.append("".join(current))
                    current = []
                if ch == "|":
                    words.append("|")
            else:
                current.append(ch)
        i += 1
    if current:
        words.append("".join(current))
    return words


def _has_shell_expansion(command: str) -> bool:
    """Return true for unquoted PowerShell variable/subexpression markers.

    Detects $var, $(...), @(...) outside of single quotes.
    """
    in_single = False
    in_double = False
    for i, ch in enumerate(command):
        if in_single:
            if ch == "'":
                in_single = False
            continue
        if in_double:
            if ch == '"':
                in_double = False
            continue
        if ch == "'":
            in_single = True
            continue
        if ch == '"':
            in_double = True
            continue
        if ch == "$":
            nxt = command[i + 1:i + 2]
            if nxt and (nxt == "(" or nxt == "{" or nxt == "_" or nxt.isalnum()):
                return True
        if ch == "@":
            nxt = command[i + 1:i + 2]
            if nxt == "(":
                return True
    return False


def _strip_cd_prefix(command: str) -> str:
    """Strip a leading ``Set-Location <dir>;`` if it's the only ; in the command.

    ``Set-Location /path; Get-Content file`` → ``Get-Content file``
    ``Set-Location /path; cmd1; cmd2`` → unchanged (multiple ;)
    """
    m = _RE_CD_PREFIX.match(command)
    if not m:
        return command
    remainder = command[m.end():]
    if _RE_SEMI.search(remainder):
        return command
    return remainder
