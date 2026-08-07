"""Git read/write and destructive-command classification."""

from __future__ import annotations

import shlex

from voidx.tooling.policy.git.constants import DENIED_SHORT_FLAGS
from voidx.tooling.policy.git.policy import git_policy_for_args


def has_denied_flag(subcommand: str, rest: list[str], denied_flags: set[str]) -> bool:
    for flag in rest:
        if flag in denied_flags:
            return True
    denied_short = DENIED_SHORT_FLAGS.get(subcommand)
    if denied_short:
        for flag in rest:
            if flag.startswith("-") and not flag.startswith("--"):
                if any(character in denied_short for character in flag[1:]):
                    return True
    return subcommand == "reflog" and bool(rest) and rest[0] == "expire"


def is_read_only_subcommand(subcommand: str, rest: list[str]) -> bool:
    args = " ".join([shlex.quote(subcommand), *(shlex.quote(arg) for arg in rest)])
    decision = git_policy_for_args({"args": args})
    return decision.allowed and decision.read_only


def is_git_read_only(args: dict) -> bool:
    decision = git_policy_for_args(args)
    return decision.allowed and decision.read_only
