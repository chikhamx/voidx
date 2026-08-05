"""Limited Git policy registry for Phase 5 external path authorization."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from voidx.permission.grants import AccessGrants, AccessIntent, resolve_access
from voidx.permission.constants import (
    DANGEROUS_CONFIG_PREFIXES,
    FORBIDDEN_GLOBAL_OPTIONS_WITH_VALUE,
    GIT_READ_POLICIES,
    GIT_REF_WRITE_FLAGS,
    GIT_WRITE_POLICIES,
)


GitPolicyAction = Literal["allow", "deny"]


@dataclass(frozen=True)
class GitCommandPolicy:
    subcommand: str
    read_only: bool


@dataclass(frozen=True)
class GitPolicyDecision:
    action: GitPolicyAction
    read_only: bool = False
    subcommand: str = ""
    rest: tuple[str, ...] = ()
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


@dataclass(frozen=True)
class GitRuntimeAccessPlan:
    worktree: Path
    git_dir: Path
    common_dir: Path
    index: Path
    object_dirs: tuple[Path, ...]
    config_files: tuple[Path, ...]
    explicit_paths: tuple[Path, ...] = ()

    def read_paths(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys((
            self.worktree,
            self.git_dir,
            self.common_dir,
            self.index,
            *self.object_dirs,
            *self.config_files,
            *self.explicit_paths,
        )))

    def requires_external_authorization(self, workspace: str, grants: AccessGrants) -> bool:
        for path in self.read_paths():
            resolution = resolve_access(
                workspace,
                str(path),
                access="read",
                access_grants=grants,
                require_exists=path.exists(),
            )
            if resolution.action != "allow":
                return True
        return False




def git_policy_for_args(args: dict) -> GitPolicyDecision:
    raw_args = _raw_git_args(args)
    try:
        tokens = shlex.split(raw_args)
    except ValueError as exc:
        return GitPolicyDecision("deny", reason=f"invalid args: {exc}")
    if not tokens:
        return GitPolicyDecision("deny", reason="empty command")

    global_error, subcommand, rest = _split_global_options(tokens)
    if global_error:
        return GitPolicyDecision("deny", reason=global_error)
    if not subcommand:
        return GitPolicyDecision("deny", reason="command is not registered")

    read_only = _registered_read_policy(subcommand, rest)
    if read_only:
        return GitPolicyDecision("allow", read_only=True, subcommand=subcommand, rest=tuple(rest))
    if _registered_write_policy(subcommand, rest):
        return GitPolicyDecision("allow", read_only=False, subcommand=subcommand, rest=tuple(rest))
    return GitPolicyDecision("deny", reason="command is not registered", subcommand=subcommand, rest=tuple(rest))


def _raw_git_args(args: dict) -> str:
    raw_args = args.get("args")
    if raw_args is None or str(raw_args).strip() == "":
        raw_args = args.get("command", "")
    return str(raw_args)


def git_sandbox_precheck(args: dict, context) -> tuple[str, str | None, tuple[AccessIntent, ...]]:
    decision = git_policy_for_args(args)
    if not decision.allowed:
        if "dangerous" in decision.reason:
            return "deny", f"git policy denied: {decision.reason}", ()
        return "defer", f"git policy deferred: {decision.reason}", ()

    path = str(args.get("path") or "")
    if not path or path == ".":
        return "allow", None, ()
    access = "read" if decision.read_only else "write"
    resolution = resolve_access(
        context.workspace,
        path,
        access=access,
        access_grants=context.access_grants,
        require_exists=True,
    )
    intents = (resolution.intent,) if resolution.intent is not None else ()
    if resolution.action == "deny":
        return "deny", resolution.reason, intents
    if resolution.action == "defer":
        return "defer", "Permission deferred to tool: outside workspace", intents
    return "allow", None, ()


def _split_global_options(tokens: list[str]) -> tuple[str, str, list[str]]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "-c":
            if index + 1 >= len(tokens):
                return "invalid global config", "", []
            return _global_config_error(tokens[index + 1]), "", []
        if token.startswith("-c") and token != "-c":
            return _global_config_error(token[2:]), "", []
        if token == "--config-env":
            if index + 1 >= len(tokens):
                return "invalid global config", "", []
            return _global_config_error(tokens[index + 1]), "", []
        if token.startswith("--config-env="):
            return _global_config_error(token.split("=", 1)[1]), "", []
        if token in FORBIDDEN_GLOBAL_OPTIONS_WITH_VALUE:
            return "global path option is not registered", "", []
        for option in FORBIDDEN_GLOBAL_OPTIONS_WITH_VALUE:
            if option.startswith("--") and token.startswith(f"{option}="):
                return "global path option is not registered", "", []
        if token.startswith("-"):
            return "global option is not registered", "", []
        return "", token, tokens[index + 1:]
    return "", "", []


def _global_config_error(config: str) -> str:
    key = config.split("=", 1)[0].strip().lower()
    if any(key == prefix.rstrip(".") or key.startswith(prefix) for prefix in DANGEROUS_CONFIG_PREFIXES):
        return "dangerous global config"
    return "global config is not registered"


def _registered_read_policy(subcommand: str, rest: list[str]) -> bool:
    if subcommand in GIT_READ_POLICIES:
        return True
    if subcommand == "config":
        return _registered_config_read(rest)
    if subcommand == "reflog":
        return bool(rest) and rest[0] in {"show", "list"}
    if subcommand in {"branch", "tag"}:
        return not any(arg in GIT_REF_WRITE_FLAGS for arg in rest)
    if subcommand == "remote":
        return not rest or all(arg in {"-v", "--verbose"} for arg in rest)
    if subcommand == "stash":
        return bool(rest) and rest[0] in {"list", "show"}
    if subcommand == "worktree":
        return bool(rest) and rest[0] == "list"
    return False


def _registered_config_read(rest: list[str]) -> bool:
    if not rest:
        return False
    read_flags = {
        "--get", "--get-all", "--get-regexp", "--get-urlmatch",
        "--list", "-l", "--show-origin", "--show-scope",
    }
    scope_flags = {"--global", "--system", "--local", "--worktree"}
    value_tokens: list[str] = []
    saw_read_flag = False
    for arg in rest:
        if arg in {"--file", "--blob"} or arg.startswith("--file=") or arg.startswith("--blob="):
            return False
        if arg in scope_flags:
            continue
        if arg in read_flags:
            saw_read_flag = True
            continue
        if arg.startswith("-"):
            return False
        value_tokens.append(arg)
    if saw_read_flag:
        return len(value_tokens) <= 1
    return len(value_tokens) == 1


def _registered_write_policy(subcommand: str, rest: list[str]) -> bool:
    if subcommand not in GIT_WRITE_POLICIES:
        return False
    if subcommand == "worktree":
        return False
    return True
