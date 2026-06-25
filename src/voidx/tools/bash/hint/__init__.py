"""Route hint detection functions for bash command routing."""

from voidx.tools.bash.hint.file import (
    _hint_find,
    _hint_read,
    _hint_tail,
    _hint_write_echo,
    _hint_write_heredoc,
)
from voidx.tools.bash.hint.git import (
    _hint_git,
)
from voidx.tools.bash.hint.search import (
    _hint_grep,
    _hint_sed,
    _parse_grep_short_flags,
    _sed_split,
)

__all__ = [
    "_hint_find",
    "_hint_git",
    "_hint_grep",
    "_hint_read",
    "_hint_sed",
    "_hint_tail",
    "_hint_write_echo",
    "_hint_write_heredoc",
    "_parse_grep_short_flags",
    "_sed_split",
]
