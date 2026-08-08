"""Bash command route hint dispatch — suggest specialized tools over raw bash."""

from __future__ import annotations

from voidx.tooling.builtin.shell.bash.core import (
    _has_shell_expansion,
    _has_shell_redirection,
    _has_unquoted_pathname_expansion,
    _RE_AMP,
    shell_words,
    _strip_cd_prefix,
)
from voidx.tooling.builtin.shell.common import RouteHint
from voidx.tooling.builtin.shell.bash.hint.file import (
    hint_find,
    hint_read,
    hint_write_echo,
    hint_write_heredoc,
)
from voidx.tooling.builtin.shell.bash.hint.git import hint_git
from voidx.tooling.builtin.shell.bash.hint.search import hint_grep, hint_sed


def try_hint(command: str) -> RouteHint | None:
    """Try to generate a specialized-tool hint for a bash command.

    Catches all exceptions and returns None — hint logic must never break bash.
    """
    try:
        return _try_hint_impl(command)
    except Exception:
        return None


def _try_hint_impl(command: str) -> RouteHint | None:
    stripped = command.strip()
    if not stripped:
        return None

    if _strip_cd_prefix(stripped) != stripped:
        return None

    if ";" in stripped or _has_shell_expansion(stripped) or _has_unquoted_pathname_expansion(stripped):
        return None
    if _RE_AMP.search(stripped):
        return None

    words = shell_words(stripped)
    if not words:
        return None

    if any(w in {"|", "|&"} for w in words):
        return None

    prog = words[0].lower()

    if prog == "cat" and "<<" in stripped:
        return hint_write_heredoc(stripped)
    if prog in ("echo", "printf") and ">" in stripped:
        return hint_write_echo(stripped, words)
    if _has_shell_redirection(words):
        return None

    if prog == "git" and len(words) >= 2:
        return hint_git(stripped, words)
    if prog in ("cat", "head", "tail"):
        return hint_read(words)
    if prog == "find":
        return hint_find(words)
    if prog in ("grep", "egrep", "fgrep", "rg"):
        return hint_grep(words)
    if prog == "sed":
        return hint_sed(words)

    return None
