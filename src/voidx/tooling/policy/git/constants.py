"""Git command classification and validation constants."""

from __future__ import annotations

import re

_BRANCH_NAME_RE = re.compile(r"^(?!\.)(?!-)[a-zA-Z0-9/_-]+(\.[a-zA-Z0-9/_-]+)*$")
_BRANCH_NAME_DENY = re.compile(r"\.\.|[@~^:\\\s]|\.lock$")
_SAFE_REMOTE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")
_SAFE_REF_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/@+-]*$")

_STRUCTURED_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "blame", "show", "branch", "remote", "tag", "stash",
})
DENIED_SUBCOMMANDS = frozenset({"filter-branch", "gc", "prune", "fsck"})
DENIED_SUBCOMMAND_FLAGS: dict[str, set[str]] = {
    "reset": {"--hard"},
    "clean": {"-x", "--force"},
    "reflog": {"expire", "--expire", "--all", "--rewrite"},
}
DENIED_SHORT_FLAGS: dict[str, set[str]] = {"clean": {"x", "d"}}
_READ_ONLY_SUBCOMMANDS = frozenset({
    "status", "log", "diff", "show", "blame", "rev-parse", "rev-list",
    "ls-files", "ls-tree", "describe", "shortlog", "cherry", "whatchanged",
    "notes", "grep", "cat-file", "name-rev", "for-each-ref",
})
REF_WRITE_FLAGS = {"-d", "-D", "-m", "-M", "--delete", "--move", "--force"}
PATHSPEC_WRITE_SUBCOMMANDS = frozenset({"add", "restore", "checkout", "rm", "mv", "reset"})

GIT_GLOBAL_OPTIONS_WITH_VALUE = {
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
}
GITREF_WRITE_FLAGS = REF_WRITE_FLAGS
GIT_READ_POLICIES = _READ_ONLY_SUBCOMMANDS
GIT_WRITE_POLICIES = frozenset({
    "add", "restore", "checkout", "switch", "rm", "mv", "reset", "commit",
    "merge", "rebase", "cherry-pick", "revert", "branch", "tag", "stash",
    "remote", "worktree", "pull", "push", "fetch",
})
DANGEROUS_CONFIG_PREFIXES = (
    "alias.", "core.askpass", "core.editor", "core.fsmonitor", "core.hookspath",
    "core.pager", "core.sshcommand", "credential.helper", "filter.", "gpg.program",
    "gpg.ssh.program", "include.", "protocol.", "sequence.editor",
)
FORBIDDEN_GLOBAL_OPTIONS_WITH_VALUE = {
    "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path",
}
