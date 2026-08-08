"""Route hint detection functions for bash command routing."""

from voidx.tooling.builtin.shell.bash.hint.file import (
    hint_find,
    hint_read,
    _hint_tail,
    hint_write_echo,
    hint_write_heredoc,
)
from voidx.tooling.builtin.shell.bash.hint.git import (
    hint_git,
)
from voidx.tooling.builtin.shell.bash.hint.search import (
    hint_grep,
    hint_sed,
    _parse_grep_short_flags,
    sed_split,
)

__all__ = [
    "hint_find",
    "hint_git",
    "hint_grep",
    "hint_read",
    "hint_sed",
    "_hint_tail",
    "hint_write_echo",
    "hint_write_heredoc",
    "_parse_grep_short_flags",
    "sed_split",
]
