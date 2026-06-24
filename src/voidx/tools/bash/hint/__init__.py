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
    _hint_git_add,
    _hint_git_blame,
    _hint_git_branch,
    _hint_git_commit,
    _hint_git_diff,
    _hint_git_log,
    _hint_git_remote,
    _hint_git_restore,
    _hint_git_show,
    _hint_git_stash,
    _hint_git_status,
    _hint_git_switch,
    _hint_git_tag,
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
    "_hint_git_add",
    "_hint_git_blame",
    "_hint_git_branch",
    "_hint_git_commit",
    "_hint_git_diff",
    "_hint_git_log",
    "_hint_git_remote",
    "_hint_git_restore",
    "_hint_git_show",
    "_hint_git_stash",
    "_hint_git_status",
    "_hint_git_switch",
    "_hint_git_tag",
    "_hint_grep",
    "_hint_read",
    "_hint_sed",
    "_hint_tail",
    "_hint_write_echo",
    "_hint_write_heredoc",
    "_parse_grep_short_flags",
    "_sed_split",
]
