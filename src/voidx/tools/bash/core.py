"""Shared primitives for bash route hint detection — no hint dependencies."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Literal

_HintableTool = Literal["read", "git", "file", "write", "replace", "glob", "grep"]

_HEREDOC_MAX_CONTENT = 200


@dataclass
class RouteHint:
    tool_id: _HintableTool
    ui_label: str
    llm_hint: str


def _shell_words(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=False, punctuation_chars=True)
        lexer.whitespace_split = True
        return [_strip_quotes(w) for w in lexer]
    except ValueError:
        return []


def _strip_quotes(word: str) -> str:
    """Strip one layer of surrounding single or double quotes (posix=False compat)."""
    if len(word) >= 2 and word[0] == word[-1] and word[0] in ("'", '"'):
        return word[1:-1]
    return word


def _has_shell_expansion(command: str) -> bool:
    """Return true for unquoted shell variable/command expansion markers."""
    in_single = False
    in_double = False
    escaped = False
    for i, ch in enumerate(command):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue
        if ch == "`" and not in_single:
            return True
        if ch == "$" and not in_single:
            nxt = command[i + 1:i + 2]
            if nxt and (nxt == "(" or nxt == "{" or nxt == "_" or nxt.isalnum()):
                return True
    return False


_RE_CD_PREFIX = re.compile(r"^cd\s+\S+\s*&&\s+")
_RE_AMP = re.compile(r"&&|\s&$")


def _strip_cd_prefix(command: str) -> str:
    """Strip a leading ``cd <dir> &&`` if it's the only && in the command.

    ``cd /path && sed ...`` → ``sed ...``
    ``cd /path && cmd1 && cmd2`` → unchanged (multiple &&)
    """
    m = _RE_CD_PREFIX.match(command)
    if not m:
        return command
    remainder = command[m.end():]
    if _RE_AMP.search(remainder):
        return command
    return remainder



_GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
})


def _git_subcommand(words: list[str]) -> tuple[str, list[str]]:
    index = 1
    while index < len(words):
        word = words[index]
        if word in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if any(word.startswith(f"{option}=") for option in _GIT_GLOBAL_OPTIONS_WITH_VALUE if option.startswith("--")):
            index += 1
            continue
        if word == "--":
            index += 1
            continue
        if word.startswith("-"):
            index += 1
            continue
        return word, words[index + 1:]
    return "", []
