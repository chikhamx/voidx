"""Bash command route hint dispatch — suggest specialized tools over raw bash."""

from __future__ import annotations

from voidx.tools.bash.core import (
    _has_shell_expansion,
    _has_unquoted_pathname_expansion,
    _RE_AMP,
    _shell_words,
    _strip_cd_prefix,
)
from voidx.tools.shell.common import RouteHint
from voidx.tools.bash.hint.file import (
    _hint_find,
    _hint_read,
    _hint_write_echo,
    _hint_write_heredoc,
)
from voidx.tools.bash.hint.git import _hint_git
from voidx.tools.bash.hint.search import _hint_grep, _hint_sed


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

    words = _shell_words(stripped)
    if not words:
        return None

    if any(w in {"|", "|&"} for w in words):
        return None

    prog = words[0].lower()

    if prog == "git" and len(words) >= 2:
        return _hint_git(stripped, words)
    if prog in ("cat", "head", "tail"):
        if prog == "cat" and "<<" in stripped:
            return _hint_write_heredoc(stripped)
        return _hint_read(words)
    if prog in ("echo", "printf") and ">" in stripped:
        return _hint_write_echo(stripped, words)
    if prog == "find":
        return _hint_find(words)
    if prog in ("grep", "egrep", "fgrep", "rg"):
        return _hint_grep(words)
    if prog == "sed":
        return _hint_sed(words)

    return None
