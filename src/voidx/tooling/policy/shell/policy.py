"""Phase 6 restricted shell policy and static access planning."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from voidx.tooling.domain.authorization import PermissionContext
from voidx.tooling.policy.filesystem.grants import resolve_access
from voidx.tooling.domain.risk import RiskAssessment, RiskLevel, RiskTag
from voidx.tooling.domain.permission import Action
from voidx.tooling.domain.process_sandbox import ProcessSandboxCapability
from voidx.tooling.policy.shell.constants import (
    DYNAMIC_MARKERS,
    GIT_GLOBAL_OPTIONS_WITH_VALUE,
    GIT_GLOBAL_OPTIONS_OPTIONAL_VALUE,
    GIT_GLOBAL_OPTIONS,
    GIT_READ_ONLY_SUBCOMMANDS,
    GIT_READ_ONLY_FLAGS,
    GIT_READ_ONLY_OPTIONS_WITH_VALUE,
    GIT_READ_ONLY_OPTIONS_WITH_OPTIONAL_VALUE,
    GIT_REF_WRITE_FLAGS,
    GIT_READ_ONLY_OUTPUT_FLAGS,
    GIT_READ_ONLY_UNSAFE_OPTIONS,
    GIT_SAFE_ENV_NAMES,
    GIT_UNSAFE_GLOBAL_OPTIONS,
    NESTED_INTERPRETERS,
    POWERSHELL_READ_COMMANDS,
    READ_COMMANDS,
    SHELL_OPERATOR_CHARS,
)


@dataclass(frozen=True)
class ShellPolicyDecision:
    allowed: bool
    read_only: bool
    reason: str = ""
    access_paths: tuple[Path, ...] = ()
    read_paths: tuple[Path, ...] = ()
    write_paths: tuple[Path, ...] = ()




def _shell_policy(
    allowed: bool,
    read_only: bool,
    reason: str = "",
    *,
    read_paths: tuple[Path, ...] = (),
    write_paths: tuple[Path, ...] = (),
) -> ShellPolicyDecision:
    reads = tuple(dict.fromkeys(read_paths))
    writes = tuple(dict.fromkeys(write_paths))
    access_paths = tuple(dict.fromkeys((*reads, *writes)))
    return ShellPolicyDecision(
        allowed=allowed,
        read_only=read_only,
        reason=reason,
        access_paths=access_paths,
        read_paths=reads,
        write_paths=writes,
    )


_EXTERNAL_ACCESS_GRANT_REASON = "shell policy deferred: external path requires access grant"


_WRITE_COMMANDS = frozenset({
    "rm", "rmdir", "unlink", "mkdir", "mktemp", "touch", "cp", "mv", "ln",
    "install", "tee", "chmod", "chown", "chgrp",
})


_GIT_WRITE_SUBCOMMANDS = frozenset({
    "add", "am", "apply", "branch", "checkout", "cherry-pick", "clean", "commit",
    "config", "fetch", "import", "init", "merge", "mv", "notes", "pull", "push",
    "rebase", "remote", "reset", "restore", "rm", "stash", "submodule", "switch",
    "tag", "update-index", "worktree", "clone", "archive",
})


_GIT_PATH_ARGUMENT_SUBCOMMANDS = frozenset({
    "add", "apply", "checkout", "diff", "grep", "ls-files", "mv", "restore", "rm",
    "show", "status",
})


_POWERSHELL_WRITE_COMMANDS = frozenset({
    "out-file", "set-content", "sc", "add-content", "ac", "tee-object", "tee",
    "new-item", "ni", "remove-item", "del", "erase", "rd", "rmdir", "move-item",
    "mv", "copy-item", "cp", "rename-item", "rni",
})


_POWERSHELL_READ_COMMANDS_WITH_PATHS = frozenset({
    "get-content", "gc", "cat", "type", "get-childitem", "gci", "dir", "ls",
    "select-string", "sls", "get-item", "gi", "test-path", "resolve-path",
})


def _paths(values: list[str] | tuple[str, ...]) -> tuple[Path, ...]:
    return tuple(Path(_clean_path_arg(value)) for value in values if _clean_path_arg(value))


def _path_union(*groups: tuple[Path, ...] | list[Path]) -> tuple[Path, ...]:
    return tuple(dict.fromkeys(path for group in groups for path in group))


@dataclass(frozen=True)
class GitCommandParts:
    environment: tuple[str, ...] = ()
    global_options: tuple[str, ...] = ()
    subcommand: str = ""
    args: tuple[str, ...] = ()
    malformed: bool = False


@dataclass(frozen=True)
class HardBlockedShellCommand:
    reason: str
    tags: tuple[RiskTag, ...]


_HARD_BLOCKED_SHELL_PATTERNS: tuple[tuple[str, str, tuple[RiskTag, ...]], ...] = (
    (r"\bsudo\b", "sudo is blocked — privilege escalation", (RiskTag.PRIVILEGE_ESCALATION,)),
    (r"\bchmod\s+.*[0]*7\d{2}\b", "chmod 7xx is blocked — world-writable permissions", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r"\bchmod\s+[0]*\d*7\b", "chmod with 7 is blocked", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r"\bchown\b", "chown is blocked", (RiskTag.PRIVILEGE_ESCALATION,)),
    (r"\bchgrp\b", "chgrp is blocked", (RiskTag.PRIVILEGE_ESCALATION,)),
    (r"\bmkfs\b", "mkfs is blocked — filesystem formatting", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r"\bdd\s+if=.*of=/dev/", "dd to /dev is blocked — raw disk write", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r">\s*/dev/sd", "write to /dev/sd* is blocked", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r"\breboot\b", "reboot is blocked", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r"\bshutdown\b", "shutdown is blocked", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r"\bpoweroff\b", "poweroff is blocked", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r"\binit\s+[06]\b", "init runlevel change is blocked", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r":\(\)\s*\{", "fork bomb pattern is blocked", (RiskTag.SYSTEM_DESTRUCTIVE,)),
    (r"\bcurl\b.*\|\s*(bash|sh|/bin/bash|/bin/sh)\b", "curl piped to shell is blocked", (RiskTag.NETWORK, RiskTag.DYNAMIC_SHELL)),
    (r"\bwget\b.*\|\s*(bash|sh|/bin/bash|/bin/sh)\b", "wget piped to shell is blocked", (RiskTag.NETWORK, RiskTag.DYNAMIC_SHELL)),
)


def hard_blocked_shell_command(command: str) -> HardBlockedShellCommand | None:
    normalized = _normalize_shell_command(command)
    for pattern, reason, tags in _HARD_BLOCKED_SHELL_PATTERNS:
        if re.search(pattern, normalized):
            return HardBlockedShellCommand(
                reason=f"Blocked: {reason}\n  command: {command.strip()[:120]}",
                tags=tags,
            )

    norm_with_separators = re.sub(r"[\r\n]+", " ; ", normalized)
    words = _shell_words_with_punctuation(norm_with_separators)
    if words is None:
        try:
            words = shlex.split(norm_with_separators, posix=True)
        except ValueError:
            return None
    for segment in _split_shell_words(words):
        git_block = _hard_blocked_git_command(segment)
        if git_block is None:
            continue
        reason, tags = git_block
        return HardBlockedShellCommand(
            reason=f"Blocked: {reason}\n  command: {command.strip()[:120]}",
            tags=tags,
        )
    return None


def _hard_blocked_git_command(words: list[str]) -> tuple[str, tuple[RiskTag, ...]] | None:
    parts = parse_git_command(words)
    subcommand = parts.subcommand
    args = list(parts.args)
    if not subcommand:
        return None
    if subcommand == "push":
        if _has_git_force_flag(args) and _push_targets_protected_branch(args):
            return "force push to main/master is blocked", (RiskTag.GIT_PUSH,)
        if _deletes_protected_remote_branch(args):
            return "deletion of main/master on remote is blocked", (RiskTag.GIT_PUSH,)
    if subcommand == "reset" and _has_git_option(args, "--hard"):
        return "git reset --hard is blocked — destructive command", (RiskTag.SYSTEM_DESTRUCTIVE,)
    if subcommand == "clean" and _git_clean_removes_untracked_files(args):
        return "git clean with force and untracked removal is blocked — destructive command", (RiskTag.SYSTEM_DESTRUCTIVE,)
    if subcommand == "filter-branch":
        return "git filter-branch is blocked — destructive history rewrite", (RiskTag.SYSTEM_DESTRUCTIVE,)
    if subcommand == "prune":
        return "git prune is blocked — destructive command", (RiskTag.SYSTEM_DESTRUCTIVE,)
    if subcommand == "gc":
        return "git gc is blocked — destructive maintenance command", (RiskTag.SYSTEM_DESTRUCTIVE,)
    if subcommand == "reflog" and args and args[0].lower() == "expire":
        return "git reflog expire is blocked — destructive command", (RiskTag.SYSTEM_DESTRUCTIVE,)
    return None


def _split_shell_words(words: list[str]) -> tuple[list[str], ...]:
    segments: list[list[str]] = []
    current: list[str] = []
    for word in words:
        if word in {";", "&&", "||", "|", "|&", "&"}:
            if current:
                segments.append(current)
                current = []
            continue
        if word in {"(", ")", "{", "}"}:
            continue
        current.append(word)
    if current:
        segments.append(current)
    return tuple(segments)


def _has_git_option(args: list[str], option: str) -> bool:
    return any(arg == option or arg.startswith(f"{option}=") for arg in args)


def _is_git_force_refspec(arg: str) -> bool:
    if arg.startswith("-"):
        return False
    if arg.startswith("+"):
        return True
    if ":" in arg:
        parts = arg.split(":", 1)
        if parts[0].startswith("+") or parts[1].startswith("+"):
            return True
    return False


def _has_git_force_flag(args: list[str]) -> bool:
    for arg in args:
        if arg in {"-f", "--force", "--force-with-lease", "--force-if-includes"}:
            return True
        if arg.startswith(("--force-with-lease=", "--force-if-includes=")):
            return True
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            if "f" in arg[1:]:
                return True
        if _is_git_force_refspec(arg):
            return True
    return False


def _push_targets_protected_branch(args: list[str]) -> bool:
    if any(arg in {"--all", "--mirror"} for arg in args):
        return True
    for arg in args:
        if arg.startswith("-"):
            continue
        ref = arg.rsplit(":", 1)[-1].lstrip("+").rstrip("/")
        if ref.rsplit("/", 1)[-1] in {"main", "master"}:
            return True
    return False


def _deletes_protected_remote_branch(args: list[str]) -> bool:
    has_delete_flag = any(
        arg in {"-d", "--delete"}
        or (arg.startswith("-") and not arg.startswith("--") and "d" in arg[1:])
        for arg in args
    )
    for arg in args:
        if arg.startswith("-"):
            continue
        if ":" in arg:
            src, dst = arg.split(":", 1)
            if not src:
                dst_clean = dst.lstrip("+").rstrip("/")
                if dst_clean.rsplit("/", 1)[-1] in {"main", "master"}:
                    return True
        elif has_delete_flag:
            clean = arg.rstrip("/")
            if clean.rsplit("/", 1)[-1] in {"main", "master"}:
                return True
    return False


def _git_clean_removes_untracked_files(args: list[str]) -> bool:
    if _has_git_force_flag(args):
        return True
    return any(
        arg in {"-x", "-X", "--interactive"}
        or (arg.startswith("-") and not arg.startswith("--") and any(flag in arg[1:] for flag in "xXd"))
        for arg in args
    )


def _normalize_shell_command(command: str) -> str:
    s = command.strip()
    s = re.sub(r"\\\s*\n", " ", s)
    s = re.sub(r"\\(.)", r"\1", s)
    s = re.sub(r"\$\([^)]*\)", "SUB", s)
    s = re.sub(r"`[^`]*`", "SUB", s)
    s = re.sub(r"''", "", s)
    return s




def shell_policy_for_command(command: str, *, shell: str = "bash") -> ShellPolicyDecision:
    risk = classify_shell_risk(command, shell=shell)
    if risk.level == RiskLevel.BLOCKED:
        return ShellPolicyDecision(False, False, risk.reason)
    stripped = command.strip()
    compound_policy = _bounded_read_only_compound_policy(stripped, shell=shell)
    if compound_policy is not None:
        return compound_policy
    if not stripped or stripped.startswith("#"):
        return ShellPolicyDecision(True, True)
    try:
        words = shlex.split(stripped, posix=shell == "bash")
    except ValueError:
        return ShellPolicyDecision(False, False, "shell policy denied unparsable command")
    if not words:
        return ShellPolicyDecision(True, True)
    decision = _powershell_policy(words) if shell == "powershell" else _bash_policy(words)
    if risk.level == RiskLevel.EXTREME:
        return _shell_policy(False, False, risk.reason, read_paths=decision.read_paths, write_paths=decision.write_paths)
    return decision

def shell_sandbox_precheck(
    args: dict,
    context: PermissionContext,
    *,
    shell: str = "bash",
) -> tuple[Action, str | None]:
    """Apply the shared shell policy and external path grants before execution."""
    command = str(args.get("command") or "")
    risk = classify_shell_risk(
        command,
        shell=shell,
        workspace=context.workspace,
    )
    if risk.level == RiskLevel.BLOCKED:
        return "deny", risk.reason

    if getattr(context, "permission_mode", None) in {"full_access", "danger-full-access"}:
        return "allow", None

    policy = shell_policy_for_command(command, shell=shell)

    for raw_path in policy.read_paths:
        resolution = resolve_access(
            context.workspace,
            str(raw_path),
            access="read",
            access_grants=context.access_grants,
            require_exists=False,
            allow_missing_write_file=False,
        )
        if resolution.action != "allow":
            return "defer", _EXTERNAL_ACCESS_GRANT_REASON

    for raw_path in policy.write_paths:
        resolution = resolve_access(
            context.workspace,
            str(raw_path),
            access="write",
            access_grants=context.access_grants,
            require_exists=False,
            allow_missing_write_file=True,
        )
        if resolution.action != "allow":
            return "defer", "shell policy deferred: external path requires writable grant"

    if not policy.allowed:
        return "defer", policy.reason


    capability = getattr(context, "process_sandbox", None) or ProcessSandboxCapability()
    if capability.supported and not capability.usable_for(shell):
        return "deny", capability.denial_reason(shell)

    return "allow", None



def classify_shell_risk(
    command: str,
    *,
    shell: str = "bash",
    workspace: str | None = None,
) -> RiskAssessment:
    stripped = command.strip()
    tool_name = "powershell" if shell == "powershell" else "bash"
    if not stripped or stripped.startswith("#"):
        return RiskAssessment.normal(tool_name=tool_name, pattern=stripped, tags=(RiskTag.SAFE_READ,), reason="empty or comment-only shell command")
    if _is_catastrophic_shell_command(stripped):
        return RiskAssessment.blocked(
            tool_name=tool_name,
            pattern=stripped,
            tags=(RiskTag.SYSTEM_DESTRUCTIVE,),
            reason="Blocked: catastrophic system command",
        )
    blocked = _blocked_shell_risk(stripped, tool_name=tool_name)
    if blocked is not None:
        return blocked
    hard_blocked = hard_blocked_shell_command(stripped)
    if hard_blocked is not None:
        return RiskAssessment.blocked(
            tool_name=tool_name,
            pattern=stripped,
            tags=hard_blocked.tags,
            reason=hard_blocked.reason,
        )
    tags: list[RiskTag] = []
    reasons: list[str] = []
    if any(marker in stripped for marker in DYNAMIC_MARKERS):
        tags.append(RiskTag.DYNAMIC_SHELL)
        reasons.append("dynamic shell syntax")
    if shell == "powershell" and any(ch in stripped for ch in "()"):
        tags.append(RiskTag.DYNAMIC_SHELL)
        reasons.append("dynamic or compound PowerShell syntax")
    if _has_shell_operator(stripped, shell=shell):
        tags.append(RiskTag.DYNAMIC_SHELL)
        reasons.append("compound shell syntax")
    try:
        words = shlex.split(stripped, posix=shell == "bash")
    except ValueError:
        return RiskAssessment.extreme(
            tool_name=tool_name,
            pattern=stripped,
            tags=(RiskTag.DYNAMIC_SHELL,),
            reason="unparsable shell command",
        )
    if _bounded_read_only_compound_policy(stripped, shell=shell) is not None:
        return RiskAssessment.normal(
            tool_name=tool_name,
            pattern=stripped,
            tags=(RiskTag.SAFE_READ,),
            reason="bounded read-only compound shell command",
        )
    if any(token in {";", "&&", "||", "|", "|&", ">", ">>", "<"} for token in words):
        tags.append(RiskTag.DYNAMIC_SHELL)
        reasons.append("compound shell operator")
    for segment_words in _compound_command_segments(stripped, shell=shell):
        segment_risk = classify_shell_risk(" ".join(segment_words), shell=shell, workspace=workspace)
        propagated_tags = tuple(tag for tag in segment_risk.tags if tag != RiskTag.SAFE_READ)
        tags.extend(propagated_tags)
        if propagated_tags:
            reasons.append("compound command segment")
    program = words[0].lower() if words else ""
    if program in NESTED_INTERPRETERS:
        tags.append(RiskTag.NESTED_INTERPRETER)
        reasons.append("nested interpreter")
        nested_command = _nested_shell_command(words)
        if nested_command is not None:
            nested_risk = classify_shell_risk(nested_command, shell=_nested_shell_kind(program), workspace=workspace)
            if nested_risk.level == RiskLevel.BLOCKED:
                return RiskAssessment.blocked(
                    tool_name=tool_name,
                    pattern=stripped,
                    tags=nested_risk.tags,
                    reason=nested_risk.reason,
                )
            tags.extend(nested_risk.tags)
            reasons.append("nested shell command")
        elif _has_external_interpreter_path(words, workspace=workspace):
            tags.append(RiskTag.EXTERNAL_PATH)
            reasons.append("external interpreter path")
        elif _uses_inline_interpreter_code(program, words):
            tags.append(RiskTag.OPAQUE_EXECUTION)
            reasons.append("inline interpreter code")
        elif program in {"bash", "sh", "zsh", "fish", "cmd", "powershell", "pwsh"} and not _has_local_script_path(words, workspace=workspace):
            tags.append(RiskTag.OPAQUE_EXECUTION)
            reasons.append("opaque nested shell command")
    if _is_git_push(words):
        tags.append(RiskTag.GIT_PUSH)
        reasons.append("git push command")
    elif _is_git_network_command(words):
        tags.append(RiskTag.NETWORK)
        reasons.append("git network command")
    if _is_dependency_install(words):
        tags.append(RiskTag.DEPENDENCY_INSTALL)
        reasons.append("dependency install command")
    if _is_network_command(words):
        tags.append(RiskTag.NETWORK)
        reasons.append("network command")
    if tags:
        deduped = tuple(dict.fromkeys(tags))
        return RiskAssessment.extreme(tool_name=tool_name, pattern=stripped, tags=deduped, reason="shell policy deferred: " + ", ".join(dict.fromkeys(reasons)))
    policy = _powershell_policy(words) if shell == "powershell" else _bash_policy(words)
    if policy.allowed:
        return RiskAssessment.normal(tool_name=tool_name, pattern=stripped, tags=(RiskTag.SAFE_READ,), reason="read-only shell command")
    return RiskAssessment.dangerous(tool_name=tool_name, pattern=stripped, tags=(RiskTag.WORKSPACE_EDIT,), reason=policy.reason)


def _blocked_shell_risk(command: str, *, tool_name: str) -> RiskAssessment | None:
    try:
        words = shlex.split(command, posix=tool_name == "bash")
    except ValueError:
        return None
    program = words[0].lower() if words else ""
    if program == "sudo":
        return RiskAssessment.blocked(
            tool_name=tool_name,
            pattern=command,
            tags=(RiskTag.PRIVILEGE_ESCALATION,),
            reason="Blocked: sudo is blocked — privilege escalation",
        )
    if program in {"reboot", "shutdown", "poweroff"}:
        return RiskAssessment.blocked(
            tool_name=tool_name,
            pattern=command,
            tags=(RiskTag.SYSTEM_DESTRUCTIVE,),
            reason=f"Blocked: {program} is blocked — system destructive command",
        )
    return None



def _has_shell_operator(command: str, *, shell: str) -> bool:
    in_single = False
    in_double = False
    escaped = False
    for ch in command:
        if escaped:
            escaped = False
            continue
        if shell == "bash" and ch == "\\" and not in_single:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue
        if not in_single and not in_double and ch in SHELL_OPERATOR_CHARS:
            return True
    return False


def _bounded_read_only_compound_policy(command: str, *, shell: str) -> ShellPolicyDecision | None:
    if shell != "bash" or not command or "\n" in command or "\r" in command:
        return None
    if any(marker in command for marker in DYNAMIC_MARKERS):
        return None
    words = _shell_words_with_punctuation(command)
    if words is None:
        return None

    segments: list[list[str]] = []
    segment: list[str] = []
    saw_operator = False
    for word in words:
        if word in {"&&", "|"}:
            if not segment:
                return None
            segments.append(segment)
            segment = []
            saw_operator = True
        elif any(char in SHELL_OPERATOR_CHARS for char in word):
            return None
        else:
            segment.append(word)
    if not saw_operator or not segment:
        return None
    segments.append(segment)

    read_paths: list[Path] = []
    write_paths: list[Path] = []
    for segment_words in segments:
        policy = _bounded_read_only_segment_policy(segment_words)
        if not policy.allowed or not policy.read_only or policy.write_paths:
            return None
        read_paths.extend(policy.read_paths)
    return _shell_policy(True, True, read_paths=tuple(read_paths))


def _bounded_read_only_segment_policy(words: list[str]) -> ShellPolicyDecision:
    if not words:
        return _shell_policy(False, False, "empty shell segment")
    program = words[0].lower()
    if program in READ_COMMANDS:
        return _bash_single_policy(words)
    if program == "find":
        return _bounded_find_policy(words[1:])
    if program in {"grep", "egrep", "fgrep", "rg"}:
        return _bounded_search_policy(program, words[1:])
    if program == "sort":
        return _bounded_sort_policy(words[1:])
    return _shell_policy(False, False, "compound segment is not registered read-only")


def _bounded_find_policy(args: list[str]) -> ShellPolicyDecision:
    if not args or args[0].startswith("-"):
        return _shell_policy(False, False, "find search path is not explicit")
    unsafe_actions = {
        "-delete",
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-fls",
        "-fprint",
        "-fprint0",
        "-fprintf",
    }
    if any(arg in unsafe_actions for arg in args):
        return _shell_policy(False, False, "find action is not read-only")
    paths: list[Path] = []
    for arg in args:
        if arg.startswith("-") or arg in {"!", "(", ")", ","}:
            break
        paths.append(Path(_clean_path_arg(arg)))
    return _shell_policy(True, True, read_paths=tuple(paths))


def _bounded_search_policy(program: str, args: list[str]) -> ShellPolicyDecision:
    if program == "rg" and any(
        arg in {"--pre", "--hostname-bin"}
        or arg.startswith(("--pre=", "--hostname-bin="))
        for arg in args
    ):
        return _shell_policy(False, False, "rg external helper is not read-only")
    return _shell_policy(True, True, read_paths=_explicit_argument_paths(args))


def _bounded_sort_policy(args: list[str]) -> ShellPolicyDecision:
    flags = {
        "-b", "--ignore-leading-blanks", "-d", "--dictionary-order",
        "-f", "--ignore-case", "-g", "--general-numeric-sort",
        "-h", "--human-numeric-sort", "-i", "--ignore-nonprinting",
        "-M", "--month-sort", "-n", "--numeric-sort", "-R", "--random-sort",
        "-r", "--reverse", "-s", "--stable", "-u", "--unique",
        "-V", "--version-sort", "-z", "--zero-terminated", "--debug",
        "--help", "--version",
    }
    value_options = {"-k", "--key", "-t", "--field-separator"}
    paths: list[Path] = []
    index = 0
    option_terminator = False
    while index < len(args):
        arg = args[index]
        if option_terminator or not arg.startswith("-") or arg == "-":
            if arg != "-":
                paths.append(Path(_clean_path_arg(arg)))
            index += 1
            continue
        if arg == "--":
            option_terminator = True
            index += 1
            continue
        if arg in flags:
            index += 1
            continue
        if arg in value_options:
            if index + 1 >= len(args) or args[index + 1] == "--":
                return _shell_policy(False, False, "sort option requires a value")
            index += 2
            continue
        if any(arg.startswith(f"{option}=") for option in ("--key", "--field-separator")):
            index += 1
            continue
        if any(arg.startswith(option) and len(arg) > len(option) for option in ("-k", "-t")):
            index += 1
            continue
        return _shell_policy(False, False, "sort option is not read-only")
    return _shell_policy(True, True, read_paths=tuple(paths))


def _explicit_argument_paths(args: list[str]) -> tuple[Path, ...]:
    paths: list[Path] = []
    for arg in args:
        candidate = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
        if _looks_like_path(candidate):
            paths.append(Path(_clean_path_arg(candidate)))
    return tuple(paths)


def _shell_words_with_punctuation(command: str) -> list[str] | None:
    try:
        lexer = shlex.shlex(command, posix=False, punctuation_chars=True)
        lexer.whitespace_split = True
        return [_strip_shell_token_quotes(word) for word in lexer]
    except ValueError:
        return None


def _strip_shell_token_quotes(word: str) -> str:
    if len(word) >= 2 and word[0] == word[-1] and word[0] in ("'", '"'):
        return word[1:-1]
    return word


_REDIR_OUTPUT_OPERATORS = frozenset({">", ">>", "&>", "&>>"})
_REDIR_INPUT_OPERATORS = frozenset({"<", "<>"})
_REDIR_FD_OPERATORS = frozenset({">&", "<&"})


def _redirection_paths(words: list[str]) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    read_paths: list[Path] = []
    write_paths: list[Path] = []
    index = 0
    while index < len(words):
        word = words[index]
        if word in _REDIR_OUTPUT_OPERATORS or word in _REDIR_INPUT_OPERATORS or word in _REDIR_FD_OPERATORS:
            if index + 1 < len(words):
                target = _clean_path_arg(words[index + 1])
                if target and not target.startswith("&") and not (
                    word in _REDIR_FD_OPERATORS and target.isdigit()
                ):
                    if word in _REDIR_OUTPUT_OPERATORS:
                        write_paths.append(Path(target))
                    elif word == "<>":
                        read_paths.append(Path(target))
                        write_paths.append(Path(target))
                    elif word in _REDIR_INPUT_OPERATORS:
                        read_paths.append(Path(target))
            index += 2
            continue
        index += 1
    return tuple(dict.fromkeys(read_paths)), tuple(dict.fromkeys(write_paths))


def _planning_segments(words: list[str]) -> tuple[list[str], ...]:
    segments: list[list[str]] = []
    current: list[str] = []
    for word in words:
        if word in {";", "&&", "||", "|", "|&"}:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(word)
    if current:
        segments.append(current)
    return tuple(segments)


def _command_start(words: list[str]) -> int:
    index = 0
    while index < len(words):
        lowered = words[index].lower()
        if lowered in {"command", "builtin"} or _is_env_assignment(words[index]):
            index += 1
            continue
        if lowered == "env":
            index += 1
            while index < len(words):
                w = words[index]
                if _is_env_assignment(w) or w in {"-i", "--ignore-environment", "-0", "--null"}:
                    index += 1
                    continue
                if w in {"-u", "--unset"}:
                    index += 2
                    continue
                if w.startswith("-u") and len(w) > 2:
                    index += 1
                    continue
                break
            continue
        break
    return index


def _bash_policy(words: list[str]) -> ShellPolicyDecision:
    segments = _planning_segments(words)
    if len(segments) > 1:
        policies = [_bash_single_policy(segment) for segment in segments]
        return _shell_policy(
            False,
            False,
            "shell policy deferred: compound shell syntax",
            read_paths=_path_union(*(policy.read_paths for policy in policies)),
            write_paths=_path_union(*(policy.write_paths for policy in policies)),
        )
    return _bash_single_policy(words)


def _bash_single_policy(words: list[str]) -> ShellPolicyDecision:
    start = _command_start(words)
    if start >= len(words):
        return _shell_policy(False, False, "unknown shell command")
    program = words[start].lower()
    args = words[start + 1:]
    redirect_reads, redirect_writes = _redirection_paths(args)

    if _is_git_program(program):
        if _is_read_only_git_command(words):
            return _shell_policy(
                True,
                True,
                read_paths=_path_union(_git_access_paths(words), redirect_reads),
                write_paths=redirect_writes,
            )
        return _shell_policy(
            False,
            False,
            f"git write command: {git_subcommand(words)}" if git_subcommand(words) else "unknown git command",
            read_paths=redirect_reads,
            write_paths=_path_union(_git_write_paths(words), redirect_writes),
        )

    if program in READ_COMMANDS:
        if program in {"cat", "head", "tail", "wc", "ls"}:
            command_reads = _file_operand_paths(args)
        else:
            command_reads = ()
        command_reads = tuple(
            path for path in command_reads
            if path not in redirect_reads and str(path) not in {"2", "1"}
        )
        return _shell_policy(
            not redirect_writes,
            not redirect_writes,
            "shell command writes through redirection" if redirect_writes else "",
            read_paths=_path_union(command_reads, redirect_reads),
            write_paths=redirect_writes,
        )

    if program in _WRITE_COMMANDS or program == "sed":
        command_reads, command_writes = _bash_write_paths(program, args)
        return _shell_policy(
            False,
            False,
            f"shell write command: {program}",
            read_paths=_path_union(command_reads, redirect_reads),
            write_paths=_path_union(command_writes, redirect_writes),
        )

    return _shell_policy(
        False,
        False,
        "unknown shell command",
        read_paths=redirect_reads,
        write_paths=redirect_writes,
    )


def _file_operand_paths(args: list[str]) -> tuple[Path, ...]:
    paths: list[Path] = []
    skip_next = False
    value_flags = {"-n", "--lines", "-c", "--bytes", "-k", "--key"}
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in _REDIR_OUTPUT_OPERATORS | _REDIR_INPUT_OPERATORS | _REDIR_FD_OPERATORS:
            skip_next = True
            continue
        if arg in value_flags:
            skip_next = True
            continue
        if arg == "-":
            continue
        if not arg.startswith("-"):
            paths.append(Path(_clean_path_arg(arg)))
            continue
        if "=" in arg:
            candidate = arg.split("=", 1)[1]
            if _looks_like_path(candidate):
                paths.append(Path(_clean_path_arg(candidate)))
    return tuple(dict.fromkeys(paths))


def _bash_write_paths(program: str, args: list[str]) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    positional = [
        Path(_clean_path_arg(arg))
        for arg in args
        if arg != "--" and not arg.startswith("-") and arg not in _REDIR_OUTPUT_OPERATORS | _REDIR_INPUT_OPERATORS | _REDIR_FD_OPERATORS
    ]
    if program in {"cp", "mv", "ln", "install"}:
        if not positional:
            return (), ()
        return tuple(positional[:-1]), (positional[-1],)
    if program in {"chmod", "chown", "chgrp"}:
        return (), tuple(positional[1:] if positional else ())
    if program == "sed":
        return (), (positional[-1],) if "-i" in args and positional else ()
    if program == "tee":
        return (), tuple(positional)
    return (), tuple(positional)


def _powershell_policy(words: list[str]) -> ShellPolicyDecision:
    segments = _planning_segments(words)
    if len(segments) > 1:
        policies = [_powershell_single_policy(segment) for segment in segments]
        return _shell_policy(
            False,
            False,
            "shell policy deferred: compound PowerShell syntax",
            read_paths=_path_union(*(policy.read_paths for policy in policies)),
            write_paths=_path_union(*(policy.write_paths for policy in policies)),
        )
    return _powershell_single_policy(words)


def _powershell_single_policy(words: list[str]) -> ShellPolicyDecision:
    if not words:
        return _shell_policy(True, True)
    program = words[0].lower()
    args = words[1:]
    redirect_reads, redirect_writes = _redirection_paths(args)

    if _is_git_program(program):
        if _is_read_only_git_command(words):
            return _shell_policy(
                True,
                True,
                read_paths=_path_union(_git_access_paths(words), redirect_reads),
                write_paths=redirect_writes,
            )
        return _shell_policy(
            False,
            False,
            f"git write command: {git_subcommand(words)}" if git_subcommand(words) else "unknown git command",
            read_paths=redirect_reads,
            write_paths=_path_union(_git_write_paths(words), redirect_writes),
        )

    if program in POWERSHELL_READ_COMMANDS:
        command_reads = _powershell_read_paths(program, args)
        return _shell_policy(
            not redirect_writes,
            not redirect_writes,
            "powershell command writes through redirection" if redirect_writes else "",
            read_paths=_path_union(command_reads, redirect_reads),
            write_paths=redirect_writes,
        )

    if program in _POWERSHELL_WRITE_COMMANDS:
        read_paths, write_paths = _powershell_write_paths(program, args)
        if program in {"tee", "tee-object"} and not write_paths and not redirect_writes:
            return _shell_policy(True, True, read_paths=redirect_reads)
        return _shell_policy(
            False,
            False,
            f"powershell write command: {program}",
            read_paths=_path_union(read_paths, redirect_reads),
            write_paths=_path_union(write_paths, redirect_writes),
        )

    return _shell_policy(
        False,
        False,
        "unknown powershell command",
        read_paths=redirect_reads,
        write_paths=redirect_writes,
    )


def _powershell_param_values(args: list[str], names: set[str]) -> tuple[Path, ...]:
    values: list[Path] = []
    index = 0
    normalized_names = {name.lower().lstrip("-") for name in names}
    while index < len(args):
        arg = args[index]
        if arg.startswith("-"):
            raw = arg[1:]
            if ":" in raw:
                name, value = raw.split(":", 1)
                if name.lower() in normalized_names and value:
                    values.append(Path(_clean_path_arg(value)))
                index += 1
                continue
            if "=" in raw:
                name, value = raw.split("=", 1)
                if name.lower() in normalized_names and value:
                    values.append(Path(_clean_path_arg(value)))
                index += 1
                continue
            if raw.lower() in normalized_names and index + 1 < len(args):
                values.append(Path(_clean_path_arg(args[index + 1])))
                index += 2
                continue
        index += 1
    return tuple(dict.fromkeys(values))


def _powershell_read_paths(program: str, args: list[str]) -> tuple[Path, ...]:
    parameter_paths = _powershell_param_values(args, {"path", "literalpath", "filepath"})
    if parameter_paths:
        return parameter_paths
    positional = [
        Path(_clean_path_arg(arg))
        for arg in args
        if not arg.startswith("-") and arg not in {">", ">>", "&>", "&>>", "<", "<>"}
    ]
    if program in {"select-string", "sls"} and len(positional) > 1:
        return tuple(positional[1:])
    return tuple(dict.fromkeys(positional))


def _powershell_write_paths(program: str, args: list[str]) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    parameter_paths = _powershell_param_values(
        args,
        {"path", "literalpath", "filepath", "destination", "newname"},
    )
    positional = [
        Path(_clean_path_arg(arg))
        for arg in args
        if not arg.startswith("-") and arg not in {">", ">>", "&>", "&>>", "<", "<>"}
    ]
    if parameter_paths:
        if program in {"copy-item", "cp", "move-item", "mv", "rename-item", "rni"} and len(positional) > 1:
            return tuple(positional[:-1]), (parameter_paths[-1],)
        return (), parameter_paths
    if program in {"copy-item", "cp", "move-item", "mv", "rename-item", "rni"} and len(positional) > 1:
        return tuple(positional[:-1]), (positional[-1],)
    return (), (positional[0],) if positional else ()


def _is_catastrophic_shell_command(command: str) -> bool:
    lowered = command.lower().strip()
    compact = " ".join(lowered.split())
    if "rm -rf /" in compact or "rm -fr /" in compact:
        targets = {"/", "/home", "~", "$home", "${home}"}
        parts = compact.replace(";", " ").split()
        if any(part.rstrip("/") in targets or part in targets for part in parts[2:]):
            return True
    if compact.startswith(("mkfs", "shutdown", "reboot", "poweroff")):
        return True
    if ":(){ :|:& };:" in compact or ":(){:|:&};:" in compact:
        return True
    if compact.startswith("dd ") and (" of=/dev/" in compact or " of=/dev/disk" in compact):
        return True
    return False
    lowered = command.lower().strip()
    compact = " ".join(lowered.split())
    if "rm -rf /" in compact or "rm -fr /" in compact:
        targets = {"/", "/home", "~", "$home", "${home}"}
        parts = compact.replace(";", " ").split()
        if any(part.rstrip("/") in targets or part in targets for part in parts[2:]):
            return True
    if compact.startswith(("mkfs", "shutdown", "reboot", "poweroff")):
        return True
    if ":(){ :|:& };:" in compact or ":(){:|:&};:" in compact:
        return True
    if compact.startswith("dd ") and (" of=/dev/" in compact or " of=/dev/disk" in compact):
        return True
    return False


def _is_dependency_install(words: list[str]) -> bool:
    if not words:
        return False
    program = words[0].lower()
    if program in {"pip", "pip3", "npm", "pnpm", "yarn", "brew", "apt", "apt-get"}:
        return any(word.lower() in {"install", "add"} for word in words[1:])
    return False


def _is_network_command(words: list[str]) -> bool:
    if not words:
        return False
    program = words[0].lower()
    if program in {"curl", "wget", "scp", "ssh", "invoke-webrequest", "invoke-restmethod", "iwr", "irm"}:
        return True
    return program in {"start-bitstransfer"}


def _compound_command_segments(command: str, *, shell: str) -> tuple[tuple[str, ...], ...]:
    if shell != "bash":
        return ()
    words = _shell_words_with_punctuation(command)
    if words is None:
        return ()
    segments: list[tuple[str, ...]] = []
    current: list[str] = []
    for word in words:
        if word in {";", "&&", "||", "|", "|&"}:
            if current:
                segments.append(tuple(current))
                current = []
            continue
        if word in {">", ">>", "<"}:
            return ()
        current.append(word)
    if current:
        segments.append(tuple(current))
    return tuple(segments[1:]) if len(segments) > 1 else ()


def _nested_shell_command(words: list[str]) -> str | None:
    if not words or words[0].lower() not in {"bash", "sh", "zsh", "fish", "cmd", "powershell", "pwsh"}:
        return None
    for index, word in enumerate(words[1:], start=1):
        if word in {"-c", "/c", "-command"} and index + 1 < len(words):
            return " ".join(words[index + 1:])
        if word.startswith("-c") and len(word) > 2:
            return word[2:]
    return None


def _nested_shell_kind(program: str) -> str:
    return "powershell" if program in {"powershell", "pwsh"} else "bash"


def _uses_inline_interpreter_code(program: str, words: list[str]) -> bool:
    inline_flags = {"-c", "-e", "--eval"}
    if program in {"python", "python3"}:
        inline_flags = {"-c"}
    return any(word in inline_flags or any(word.startswith(f"{flag}=") for flag in inline_flags) for word in words[1:])


def _has_local_script_path(words: list[str], *, workspace: str | None) -> bool:
    if workspace is None or len(words) < 2:
        return False
    for word in words[1:]:
        if word.startswith("-"):
            continue
        raw = Path(_clean_path_arg(word)).expanduser()
        if not raw.is_absolute():
            raw = Path(workspace) / raw
        try:
            raw.resolve(strict=False).relative_to(Path(workspace).expanduser().resolve())
            return True
        except (OSError, RuntimeError, ValueError):
            return False
    return False


def _has_external_interpreter_path(words: list[str], *, workspace: str | None) -> bool:
    if workspace is None or not words:
        return False
    workspace_path = Path(workspace).expanduser().resolve()
    for word in words[1:]:
        if word.startswith("-"):
            continue
        raw = Path(_clean_path_arg(word)).expanduser()
        if not raw.is_absolute():
            return False
        try:
            raw.resolve(strict=False).relative_to(workspace_path)
            return False
        except (OSError, RuntimeError, ValueError):
            return True
    return False


def _is_git_push(words: list[str]) -> bool:
    return git_subcommand(words) == "push"


def _is_git_network_command(words: list[str]) -> bool:
    return git_subcommand(words) in {"clone", "fetch", "ls-remote", "pull"}


def parse_git_command(words: list[str]) -> GitCommandParts:
    """Parse a tokenized Git invocation without dropping option values."""
    if not words:
        return GitCommandParts()

    index = 0
    environment: list[str] = []
    malformed = False
    env_wrapper = False

    while index < len(words):
        word = words[index]
        lowered = word.lower()
        if lowered in {"command", "builtin"}:
            index += 1
            continue
        if _is_env_assignment(word):
            environment.append(word)
            index += 1
            continue
        if lowered != "env":
            break

        env_wrapper = True
        environment.append("env")
        index += 1
        while index < len(words):
            word = words[index]
            if _is_env_assignment(word):
                environment.append(word)
                index += 1
                continue
            if word in {"-i", "--ignore-environment", "-0", "--null"}:
                environment.append(word)
                index += 1
                continue
            if word in {"-u", "--unset"}:
                environment.append(word)
                if index + 1 >= len(words):
                    malformed = True
                    index += 1
                else:
                    environment.append(words[index + 1])
                    index += 2
                continue
            if word.startswith("-u") and len(word) > 2:
                environment.append(word)
                index += 1
                continue
            if word.startswith("-"):
                environment.append(word)
                malformed = True
                index += 1
                continue
            break

    if index >= len(words) or not _is_git_program(words[index]):
        return GitCommandParts(
            environment=tuple(environment),
            malformed=malformed or env_wrapper,
        )

    index += 1
    global_options: list[str] = []
    end_of_options = False
    while index < len(words):
        word = words[index]
        if end_of_options:
            return GitCommandParts(
                environment=tuple(environment),
                global_options=tuple(global_options),
                subcommand=word.lower(),
                args=tuple(words[index + 1:]),
                malformed=malformed,
            )
        if word == "--":
            global_options.append(word)
            end_of_options = True
            index += 1
            continue
        if not word.startswith("-") or word == "-":
            return GitCommandParts(
                environment=tuple(environment),
                global_options=tuple(global_options),
                subcommand=word.lower(),
                args=tuple(words[index + 1:]),
                malformed=malformed,
            )

        option_name, attached_value = _split_git_global_option(word)
        if option_name in GIT_GLOBAL_OPTIONS_WITH_VALUE:
            if attached_value is not None or option_name in GIT_GLOBAL_OPTIONS_OPTIONAL_VALUE:
                global_options.append(_format_git_global_option(option_name, attached_value))
                index += 1
                continue
            if index + 1 >= len(words):
                global_options.append(option_name)
                malformed = True
                index += 1
                continue
            global_options.append(f"{option_name}={words[index + 1]}")
            index += 2
            continue

        if option_name not in GIT_GLOBAL_OPTIONS:
            malformed = True
        global_options.append(word)
        index += 1

    return GitCommandParts(
        environment=tuple(environment),
        global_options=tuple(global_options),
        malformed=malformed,
    )


def _is_git_program(word: str) -> bool:
    name = Path(word).name.lower()
    return name in {"git", "git.exe"}


def _format_git_global_option(name: str, value: str | None) -> str:
    if value is None:
        return name
    return f"{name}={value}"


def _split_git_global_option(word: str) -> tuple[str, str | None]:
    if word.startswith("--"):
        if "=" in word:
            return word.split("=", 1)[0], word.split("=", 1)[1]
        return word, None
    if word in GIT_GLOBAL_OPTIONS_WITH_VALUE:
        return word, None
    for option in ("-C", "-c"):
        if word.startswith(option) and len(word) > len(option):
            value = word[len(option):]
            if value.startswith("="):
                value = value[1:]
            return option, value
    return word, None


def git_subcommand_and_args(words: list[str]) -> tuple[str, list[str]]:
    parts = parse_git_command(words)
    return parts.subcommand, [*parts.global_options, *parts.args]


def git_subcommand(words: list[str]) -> str:
    return parse_git_command(words).subcommand


def is_read_only_git_command(words: list[str]) -> bool:
    """Return whether a tokenized Git invocation is explicitly read-only."""
    parts = parse_git_command(words)
    if not parts.subcommand:
        return _is_git_help_or_version(parts)
    if parts.malformed:
        return False
    if any(_is_unsafe_git_environment(value) for value in parts.environment):
        return False
    if any(_is_unsafe_git_global_option(option) for option in parts.global_options):
        return False

    sub = parts.subcommand
    args = list(parts.args)
    if sub == "config":
        return _is_read_only_git_config(args)
    if sub == "stash":
        return bool(args) and args[0] in {"list", "show"} and _validate_git_args(
            sub, args[1:], flags={"-p", "--patch", "--stat", "--name-only", "--name-status"},
            value_options={"--format", "--pretty"}, allow_positionals=True,
        )
    if sub == "bisect":
        return bool(args) and args[0] in {"log", "view", "visualize"} and _validate_git_args(
            sub, args[1:], flags=set(), value_options=set(), allow_positionals=False,
        )
    if sub in {"branch", "tag"}:
        return _is_read_only_git_ref_command(sub, args)
    if sub == "remote":
        if args in ([], ["-v"], ["--verbose"]):
            return True
        if len(args) == 2 and args[0] == "show" and not args[1].startswith("-"):
            return True
        if len(args) == 2 and args[0] == "get-url" and not args[1].startswith("-"):
            return True
        if len(args) == 3 and args[0] == "get-url" and args[1] in {"--all", "--push"} and not args[2].startswith("-"):
            return True
        return False
    if sub == "worktree":
        return bool(args) and args[0] == "list" and _validate_git_args(
            sub, args[1:], flags={"-v", "--verbose", "--porcelain"},
            value_options=set(), allow_positionals=False,
        )
    if sub == "reflog":
        if args and args[0] not in {"show", "list"}:
            return False
        return _validate_git_args(
            sub, args[1:] if args and args[0] in {"show", "list"} else args,
            flags={"--all", "--single-worktree", "--updateref", "--stale-fix", "--rewrite"},
            value_options={"--expire", "--expire-unreachable", "--format"},
            allow_positionals=True,
        )
    if sub not in GIT_READ_ONLY_SUBCOMMANDS:
        return False
    return _validate_git_args(
        sub,
        args,
        flags=GIT_READ_ONLY_FLAGS.get(sub, frozenset()),
        value_options=GIT_READ_ONLY_OPTIONS_WITH_VALUE.get(sub, frozenset()),
        optional_value_options=GIT_READ_ONLY_OPTIONS_WITH_OPTIONAL_VALUE.get(sub, frozenset()),
        allow_positionals=True,
    )


def _is_read_only_git_command(words: list[str]) -> bool:
    return is_read_only_git_command(words)


def _is_git_help_or_version(parts: GitCommandParts) -> bool:
    if parts.malformed or parts.environment:
        return False
    tokens = [*parts.global_options, *parts.args]
    return bool(tokens) and all(token in {"-v", "--version", "-h", "--help"} for token in tokens)


def _is_unsafe_git_environment(assignment: str) -> bool:
    if assignment == "env":
        return True
    if not _is_env_assignment(assignment):
        return True
    name = assignment.split("=", 1)[0]
    return name not in GIT_SAFE_ENV_NAMES


def _is_unsafe_git_global_option(option: str) -> bool:
    name = option.split("=", 1)[0]
    return name in GIT_UNSAFE_GLOBAL_OPTIONS


def _validate_git_args(
    subcommand: str,
    args: list[str],
    *,
    flags: set[str] | frozenset[str],
    value_options: set[str] | frozenset[str],
    optional_value_options: set[str] | frozenset[str] = frozenset(),
    allow_positionals: bool,
) -> bool:
    index = 0
    option_terminator = False
    while index < len(args):
        arg = args[index]
        if arg == "--":
            option_terminator = True
            index += 1
            continue
        if option_terminator or not arg.startswith("-") or arg == "-":
            if not allow_positionals:
                return False
            index += 1
            continue
        if _is_git_output_write_option(subcommand, arg) or _is_git_read_only_unsafe_option(arg):
            return False

        name, attached = _split_git_argument_option(subcommand, arg, flags, value_options, optional_value_options)
        if name is None:
            return False
        if attached is not None:
            index += 1
            continue
        if name in value_options:
            if index + 1 >= len(args) or args[index + 1] == "--":
                return False
            index += 2
            continue
        index += 1
    return True


def _split_git_argument_option(
    subcommand: str,
    arg: str,
    flags: set[str] | frozenset[str],
    value_options: set[str] | frozenset[str],
    optional_value_options: set[str] | frozenset[str],
) -> tuple[str | None, str | None]:
    if arg.startswith("--"):
        if "=" in arg:
            name, value = arg.split("=", 1)
            if name in value_options or name in optional_value_options:
                return name, value
            return None, None
        if arg in flags or arg in value_options or arg in optional_value_options:
            return arg, None
        return None, None

    if arg in flags or arg in value_options or arg in optional_value_options:
        return arg, None

    short_options = sorted(
        (option for option in (*value_options, *optional_value_options) if option.startswith("-") and not option.startswith("--")),
        key=len,
        reverse=True,
    )
    for option in short_options:
        if arg.startswith(option) and len(arg) > len(option):
            return option, arg[len(option):]

    if arg.startswith("-") and len(arg) >= 2 and subcommand_supports_numeric_short_option(subcommand, arg):
        return arg, arg[1:]

    if arg.startswith("-") and len(arg) > 2:
        short_flags = {flag for flag in flags if flag.startswith("-") and not flag.startswith("--") and len(flag) == 2}
        if all(f"-{char}" in short_flags for char in arg[1:]):
            return arg, None
    return None, None


def subcommand_supports_numeric_short_option(subcommand: str, arg: str) -> bool:
    return subcommand in {"log", "rev-list"} and arg[1:].isdigit()


def _is_git_output_write_option(subcommand: str, arg: str) -> bool:
    if subcommand == "ls-files" and arg == "-o":
        return False
    name = arg.split("=", 1)[0]
    return name in GIT_READ_ONLY_OUTPUT_FLAGS


def _is_git_read_only_unsafe_option(arg: str) -> bool:
    name = arg.split("=", 1)[0]
    return name in GIT_READ_ONLY_UNSAFE_OPTIONS or name in {"-O"}


def _is_read_only_git_config(args: list[str]) -> bool:
    read_flags = {
        "--get", "--get-all", "--get-regexp", "--get-urlmatch",
        "--list", "-l", "--show-origin", "--show-scope", "--name-only",
        "--show-names", "--fixed-value",
    }
    scope_flags = {"--global", "--system", "--local", "--worktree"}
    value_options = {"--get-color", "--get-colorbool"}
    values: list[str] = []
    saw_read_flag = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in read_flags or arg in scope_flags:
            saw_read_flag = saw_read_flag or arg in read_flags
            index += 1
            continue
        if arg in {"--file", "--blob", "--edit", "--unset", "--unset-all", "--rename-section", "--remove-section"}:
            return False
        if arg.startswith("-"):
            name, attached = arg.split("=", 1) if "=" in arg else (arg, None)
            if name in value_options and attached is not None:
                index += 1
                continue
            return False
        values.append(arg)
        index += 1
    if saw_read_flag:
        return len(values) <= 2
    return len(values) == 1


def _is_read_only_git_ref_command(subcommand: str, args: list[str]) -> bool:
    flags = {
        "branch": {"-a", "--all", "-r", "--remotes", "-l", "--list", "-v", "--verbose", "--column", "--no-column", "--contains", "--no-contains", "--merged", "--no-merged"},
        "tag": {"-l", "--list", "-n", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--column", "--no-column", "--sort", "--format", "--cleanup"},
    }[subcommand]
    value_options = {
        "branch": {"--contains", "--no-contains", "--merged", "--no-merged", "--sort", "--format"},
        "tag": {"-n", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort", "--format"},
    }[subcommand]
    list_mode = not args or any(arg in {"-l", "--list"} for arg in args)
    return _validate_git_args(
        subcommand,
        args,
        flags=flags,
        value_options=value_options,
        allow_positionals=list_mode,
    )


def _has_git_read_only_unsafe_option(args: list[str]) -> bool:
    return any(
        _is_git_output_write_option("", arg) or _is_git_read_only_unsafe_option(arg)
        for arg in args
    )


def _git_access_paths(words: list[str]) -> tuple[Path, ...]:
    parts = parse_git_command(words)
    paths: list[Path] = []
    for option in parts.global_options:
        name, value = _split_git_global_option(option)
        if name in {"-C", "--git-dir", "--work-tree"} and value:
            paths.append(Path(_clean_path_arg(value)))

    after_separator = False
    for arg in parts.args:
        if arg == "--":
            after_separator = True
            continue
        if arg.startswith("-") or "=" in arg:
            continue
        if after_separator or _looks_like_path(arg):
            paths.append(Path(_clean_path_arg(arg)))
    return tuple(dict.fromkeys(paths))


def _git_write_paths(words: list[str]) -> tuple[Path, ...]:
    parts = parse_git_command(words)
    paths: list[Path] = []
    for option in parts.global_options:
        name, value = _split_git_global_option(option)
        if name in {"-C", "--git-dir", "--work-tree"} and value:
            paths.append(Path(_clean_path_arg(value)))

    after_separator = False
    index = 0
    args = parts.args
    while index < len(args):
        arg = args[index]
        if arg == "--":
            after_separator = True
            index += 1
            continue
        if not after_separator:
            if arg in {"-o", "--output"} and index + 1 < len(args):
                paths.append(Path(_clean_path_arg(args[index + 1])))
                index += 2
                continue
            if arg.startswith("--output="):
                paths.append(Path(_clean_path_arg(arg.split("=", 1)[1])))
                index += 1
                continue
            if arg.startswith("-o") and len(arg) > 2 and parts.subcommand != "ls-files":
                paths.append(Path(_clean_path_arg(arg[2:])))
                index += 1
                continue
            if arg.startswith("-") or "=" in arg:
                index += 1
                continue
        if after_separator or _looks_like_path(arg):
            paths.append(Path(_clean_path_arg(arg)))
        index += 1
    return tuple(dict.fromkeys(paths))


def _is_env_assignment(word: str) -> bool:
    return "=" in word and not word.startswith("=") and word.split("=", 1)[0].isidentifier()


def _clean_path_arg(value: str) -> str:
    return value.strip().strip("'").strip('"').strip("`")


def _looks_like_path(value: str) -> bool:
    value = _clean_path_arg(value)
    if not value or value.startswith("-"):
        return False
    return value.startswith(("/", "~", ".")) or "/" in value or "\\" in value or "." in Path(value).name
