"""Git tool constants, regex patterns, and subcommand classification sets."""

from __future__ import annotations

import re

GIT_TIMEOUT_SECONDS = 15
GIT_REMOTE_TIMEOUT_SECONDS = 60
DIFF_HUNK_MAX_CHARS = 12_000
HOOK_OUTPUT_MAX_CHARS = 4000
LOG_LIMIT_MAX = 50
BLAME_RANGE_MAX = 200
_BRANCH_NAME_RE = re.compile(r"^(?!\.)(?!-)[a-zA-Z0-9/_-]+(\.[a-zA-Z0-9/_-]+)*$")
_BRANCH_NAME_DENY = re.compile(r"\.\.|[@~^:\\\s]|\.lock$")
_SAFE_REMOTE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")
_SAFE_REF_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/@+-]*$")

# Subcommands that get structured JSON output via dedicated parsers.
_STRUCTURED_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "blame", "show", "branch", "remote", "tag", "stash",
})

# Subcommands that are always denied (destructive / irreversible).
_DENIED_SUBCOMMANDS = frozenset({
    "filter-branch", "gc", "prune", "fsck",
})

# Subcommand + flag combinations that are denied (destructive / irreversible).
# Maps subcommand to a set of flags; if any flag is present, the command is denied.
_DENIED_SUBCOMMAND_FLAGS: dict[str, set[str]] = {
    "reset": {"--hard"},
    "clean": {"-x", "--force"},
    "reflog": {"expire", "--expire", "--all", "--rewrite"},
}
# Subcommands where any denied short flag (even standalone) triggers denial.
# For clean: -x removes ignored files, -d removes untracked directories.
_DENIED_SHORT_FLAGS: dict[str, set[str]] = {
    "clean": {"x", "d"},
}

# Subcommands that are always read-only (no approval needed).
_READ_ONLY_SUBCOMMANDS = frozenset({
    "status", "log", "diff", "show", "blame", "rev-parse", "rev-list",
    "ls-files", "ls-tree", "describe", "shortlog", "cherry",
    "whatchanged", "notes", "grep", "cat-file", "name-rev", "for-each-ref",
})

# Write flags for branch/tag subcommands.
_REF_WRITE_FLAGS = {"-d", "-D", "-m", "-M", "--delete", "--move", "--force"}

# Write subcommands whose pathspec arguments must be validated against workspace.
_PATHSPEC_WRITE_SUBCOMMANDS = frozenset({
    "add", "restore", "checkout", "rm", "mv", "reset",
})
