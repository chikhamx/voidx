"""Centralized module-level constants for the permission system.

Single source of truth for action literals, shell/git/sandbox policy sets,
and preset deny tags. Previously these were scattered (and in some cases
duplicated) across schema.py, shell_policy.py, git_policy.py, rules.py,
sandbox.py, and presets.py.
"""

from __future__ import annotations

import re

from voidx.permission.risk import RiskTag
from voidx.permission.schema import Action

# Re-export Action so callers can import all decision literals from one place.
__all__ = [
    "Action",
    "GIT_GLOBAL_OPTIONS_WITH_VALUE",
    "GIT_REF_WRITE_FLAGS",
    "GIT_READ_POLICIES",
    "GIT_WRITE_POLICIES",
    "DANGEROUS_CONFIG_PREFIXES",
    "FORBIDDEN_GLOBAL_OPTIONS_WITH_VALUE",
    "READ_COMMANDS",
    "POWERSHELL_READ_COMMANDS",
    "DYNAMIC_MARKERS",
    "NESTED_INTERPRETERS",
    "SHELL_OPERATOR_CHARS",
    "REDIR_PATTERNS",
    "FS_WRITE_COMMANDS",
    "FILE_PATTERN_TOOLS",
    "PROJECT_TRUSTED_DENY_TAGS",
]


# ── Git global options that consume a following argument ──────────────
# Previously duplicated in rules.py and sandbox.py.
GIT_GLOBAL_OPTIONS_WITH_VALUE = {
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
}

# ── Git ref-write flags ───────────────────────────────────────────────
# Previously duplicated in git_policy.py (_REF_WRITE_FLAGS) and
# rules.py (local write_flags inside _is_read_only_git_ref_command).
GIT_REF_WRITE_FLAGS = {"-d", "-D", "-m", "-M", "--delete", "--move", "--force"}

# ── Git subcommand policies ───────────────────────────────────────────
GIT_READ_POLICIES = frozenset({
    "status",
    "log",
    "diff",
    "show",
    "blame",
    "rev-parse",
    "rev-list",
    "ls-files",
    "ls-tree",
    "describe",
    "shortlog",
    "cherry",
    "whatchanged",
    "notes",
    "grep",
    "cat-file",
    "name-rev",
    "for-each-ref",
})

GIT_WRITE_POLICIES = frozenset({
    "add",
    "restore",
    "checkout",
    "switch",
    "rm",
    "mv",
    "reset",
    "commit",
    "merge",
    "rebase",
    "cherry-pick",
    "revert",
    "branch",
    "tag",
    "stash",
    "remote",
    "worktree",
    "pull",
    "push",
    "fetch",
})

# ── Dangerous git config key prefixes ─────────────────────────────────
DANGEROUS_CONFIG_PREFIXES = (
    "alias.",
    "core.askpass",
    "core.editor",
    "core.fsmonitor",
    "core.hookspath",
    "core.pager",
    "core.sshcommand",
    "credential.helper",
    "filter.",
    "gpg.program",
    "gpg.ssh.program",
    "include.",
    "protocol.",
    "sequence.editor",
)

# ── Forbidden git global options that take a value ────────────────────
FORBIDDEN_GLOBAL_OPTIONS_WITH_VALUE = {
    "-C",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--exec-path",
}

# ── Shell policy sets ─────────────────────────────────────────────────
READ_COMMANDS = {"cat", "head", "tail", "wc", "ls", "pwd", "echo", "printf"}

POWERSHELL_READ_COMMANDS = {
    "get-content", "gc", "cat", "type",
    "get-childitem", "gci", "dir", "ls",
    "write-output", "echo",
}

DYNAMIC_MARKERS = ("$", "`", "<(", ">(")

NESTED_INTERPRETERS = {
    "bash", "sh", "zsh", "fish",
    "cmd", "powershell", "pwsh",
    "python", "python3", "node", "ruby", "perl",
}

SHELL_OPERATOR_CHARS = {";", "|", "<", ">", "&", "\n", "\r"}

# ── Sandbox: bash write-target extraction ─────────────────────────────
# Each regex captures the write target path in group 1.
REDIR_PATTERNS = [
    re.compile(r"\d?\s*>>?\s*(\S+)"),
    re.compile(r"\|\s*tee(?:\s+-a)?\s+(\S+)"),
    re.compile(r"\bof=(\S+)"),
]

# Destructive filesystem commands → arg index of write target.
# Positive N: Nth arg (1-based). Negative N: last N args are sources,
# final arg is destination. 0: no filesystem layout change.
FS_WRITE_COMMANDS = {
    "rm": 1,
    "cp": -1,
    "mv": -1,
    "ln": -1,
    "mkdir": 1,
    "touch": 1,
    "install": -1,
    "tee": 1,
    "chmod": 0,
    "chown": 0,
    "chgrp": 0,
}

# ── Tool name → file-path-argument pattern ────────────────────────────
FILE_PATTERN_TOOLS = {
    "read", "write", "replace", "lsp_format",
    "lsp",
}

# ── Preset deny tags ──────────────────────────────────────────────────
PROJECT_TRUSTED_DENY_TAGS = frozenset({
    RiskTag.NETWORK,
    RiskTag.EXTERNAL_PATH,
    RiskTag.GIT_PUSH,
    RiskTag.SYSTEM_DESTRUCTIVE,
    RiskTag.PRIVILEGE_ESCALATION,
    RiskTag.OPAQUE_EXECUTION,
})
