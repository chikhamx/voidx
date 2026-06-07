"""Permission rules and tool capability classification."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import Enum

from voidx.permission.schema import Rule, Ruleset


class PermissionCapability(str, Enum):
    READ_TOOLS = "read_tools"
    FILE_WRITE = "file_write"
    FILE_FORMAT = "file_format"
    BASH_READ = "bash_read"
    BASH_WRITE = "bash_write"
    GIT_READ = "git_read"
    GIT_WRITE = "git_write"
    AGENT_READONLY = "agent_readonly"
    AGENT_IMPLEMENT = "agent_implement"
    MCP_TOOLS = "mcp_tools"
    OTHER = "other"


BASIC_RULES: Ruleset = [
    Rule(permission="read", pattern="*", action="allow"),
    Rule(permission="glob", pattern="*", action="allow"),
    Rule(permission="grep", pattern="*", action="allow"),
    Rule(permission="webfetch", pattern="*", action="allow"),
    Rule(permission="websearch", pattern="*", action="allow"),
    Rule(permission="todo", pattern="*", action="allow"),
    Rule(permission="on_intent", pattern="*", action="allow"),
    Rule(permission="clarify", pattern="*", action="allow"),
    Rule(permission="plan_checkpoint", pattern="*", action="allow"),
    Rule(permission="task_status", pattern="*", action="allow"),
    Rule(permission="repo_map", pattern="*", action="allow"),
    Rule(permission="lsp_diagnostics", pattern="*", action="allow"),
    Rule(permission="lsp_symbols", pattern="*", action="allow"),
    Rule(permission="lsp_definition", pattern="*", action="allow"),
    Rule(permission="lsp_references", pattern="*", action="allow"),
    Rule(permission="agent", pattern="*", action="allow"),
    Rule(permission="write", pattern="*", action="ask"),
    Rule(permission="edit", pattern="*", action="ask"),
    Rule(permission="git", pattern="write", action="ask"),
    Rule(permission="bash", pattern="*", action="ask"),
    Rule(permission="lsp_format", pattern="*", action="ask"),
    Rule(permission="agent", pattern="implement", action="ask"),
    Rule(permission="mcp__*", pattern="*", action="ask"),
    Rule(permission="mcp/*", pattern="*", action="ask"),
]


@dataclass(frozen=True)
class ClassifiedToolCall:
    tool_call: dict
    name: str
    args: dict
    pattern: str
    capability: PermissionCapability


def classify_tool_call(tool_call: dict) -> ClassifiedToolCall:
    name = repair_tool_name(str(tool_call.get("name", "")))
    args = tool_call.get("args", {})
    if not isinstance(args, dict):
        args = {}
    repaired = {**tool_call, "name": name, "args": args}
    pattern = build_pattern(name, args)
    return ClassifiedToolCall(
        tool_call=repaired,
        name=name,
        args=args,
        pattern=pattern,
        capability=capability_for_tool(name, args),
    )


def tool_call_from_pattern(tool: str, pattern: str = "*") -> dict:
    name = repair_tool_name(tool)
    if name == "bash":
        args = {"command": pattern}
    elif name == "agent":
        args = {"agent": pattern}
    elif name in _FILE_PATTERN_TOOLS:
        args = {"file_path": pattern}
    else:
        args = {}
    return {"name": name, "args": args}


def repair_tool_name(tool: str) -> str:
    tool_map = {
        "Read": "read", "Write": "write", "Edit": "edit",
        "MultiEdit": "edit", "multiEdit": "edit", "multi_edit": "edit",
        "Glob": "glob", "Grep": "grep", "Bash": "bash",
        "Agent": "agent", "TodoWrite": "todo", "Todo": "todo",
        "WebFetch": "webfetch", "WebSearch": "websearch",
        "read_file": "read", "write_file": "write",
        "edit_file": "edit", "shell": "bash",
        "readfile": "read", "writefile": "write",
        "search": "grep", "find": "glob",
        "RepoMap": "repo_map", "repomap": "repo_map", "Repo_map": "repo_map",
        "LspDiagnostics": "lsp_diagnostics", "LspSymbols": "lsp_symbols",
        "LspDefinition": "lsp_definition", "LspReferences": "lsp_references",
        "LspFormat": "lsp_format",
    }
    return tool_map.get(tool, tool_map.get(tool.lower(), tool))


def build_pattern(tool: str, args: dict) -> str:
    if tool == "bash":
        return str(args.get("command", "*"))
    if tool in _FILE_PATTERN_TOOLS:
        return str(args.get("file_path", "*"))
    if tool == "agent":
        return delegated_agent(args) or "*"
    if tool == "git":
        return "read" if _is_read_only_git_tool_command(args) else "write"
    return "*"


def delegated_agent(args: dict) -> str:
    return str(args.get("agent") or "")


def is_safe_bash(command: str) -> bool:
    stripped = command.strip()
    if not stripped or stripped.startswith("#"):
        return True
    if "$(" in stripped or "`" in stripped:
        return False

    words = _shell_words(stripped)
    if words is None:
        return False
    if _has_write_redirection(words):
        return False

    segments = _bash_segments(words)
    return bool(segments) and all(_is_safe_bash_segment(segment) for segment in segments)


def _shell_words(command: str) -> list[str] | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return None


def _has_write_redirection(words: list[str]) -> bool:
    write_redirections = {">", ">>", ">|", "&>", "&>>"}
    for index, word in enumerate(words):
        if word in write_redirections:
            if index + 1 < len(words) and words[index + 1].startswith("&"):
                continue
            return True
    return False


def _bash_segments(words: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    segment: list[str] = []
    for word in words:
        if word in {";", "&&", "||", "|", "|&"}:
            if segment:
                segments.append(segment)
            segment = []
            continue
        segment.append(word)
    if segment:
        segments.append(segment)
    return segments


def _is_safe_bash_segment(words: list[str]) -> bool:
    prog, args = _program_and_args(words)
    if not prog:
        return True
    prog = prog.lower()

    if prog in {"command", "builtin"}:
        return bool(args) and _is_safe_bash_segment(args)
    if prog == "env":
        return _is_safe_env(args)

    if prog == "git" and len(words) > 1:
        sub, sub_args = _git_subcommand(args)
        if not sub:
            return True
        read_only_git = {
            "status", "log", "diff", "show", "blame", "rev-parse", "rev-list",
            "ls-files", "ls-tree", "describe", "shortlog", "reflog", "cherry",
            "whatchanged", "notes", "grep",
        }
        if sub in read_only_git:
            return True
        if sub == "config":
            return _is_read_only_git_config(sub_args)
        if sub == "stash":
            return bool(sub_args) and sub_args[0] in ("list", "show")
        if sub == "bisect":
            return bool(sub_args) and sub_args[0] in ("log", "view", "visualize")
        if sub in ("branch", "tag"):
            return _is_read_only_git_ref_command(sub, sub_args)
        if sub == "remote":
            return not sub_args or "-v" in sub_args or "--verbose" in sub_args
        if sub == "worktree":
            return bool(sub_args) and sub_args[0] == "list"
        return False

    if prog == "gh" and args:
        sub = args[0]
        if sub == "pr":
            return len(args) > 1 and args[1] in ("view", "list", "status", "checks", "diff")
        if sub == "issue":
            return len(args) > 1 and args[1] in ("view", "list", "status")
        if sub == "api":
            cmd_upper = " ".join(args).upper()
            if "-X" in cmd_upper or "--METHOD" in cmd_upper:
                return "GET" in cmd_upper
            return True
        if sub in ("auth", "config", "completion", "secret"):
            return len(args) == 1 or (len(args) > 1 and args[1] in ("list", "status", "view"))
        return False

    if prog == "find":
        return not any(arg in {"-delete", "-exec", "-execdir", "-ok", "-okdir"} for arg in args)
    if prog == "sort":
        return "-o" not in args and not any(arg.startswith("--output") for arg in args)

    read_only = {
        "ls", "dir", "cat", "head", "tail", "wc", "which", "where", "whereis",
        "echo", "printf", "pwd", "date", "whoami", "uname", "printenv",
        "df", "du", "sort", "uniq", "cut", "tr", "column", "less", "more",
        "grep", "egrep", "fgrep", "rg", "file", "stat", "od",
        "true", "false", "test", "[", "type", "basename", "dirname",
        "realpath", "readlink", "hostname", "id", "groups", "logname",
        "uptime", "free", "swapon", "lscpu", "lsblk", "lspci", "lsusb",
    }
    if prog in read_only:
        return True

    if prog in ("pip", "pip3") and args:
        return args[0] in ("list", "show", "freeze", "config", "cache")
    if prog in ("npm", "npx") and args:
        return args[0] in ("list", "ls", "view", "info", "outdated")
    if prog == "cargo" and args:
        return args[0] in ("search", "doc", "readme")
    if prog == "go" and args:
        return args[0] in ("list", "doc", "version", "env")

    return False


def _program_and_args(words: list[str]) -> tuple[str, list[str]]:
    for index, word in enumerate(words):
        if _is_assignment(word):
            continue
        return word, words[index + 1:]
    return "", []


def _is_assignment(word: str) -> bool:
    if "=" not in word or word.startswith("="):
        return False
    return word.split("=", 1)[0].isidentifier()


def _is_safe_env(args: list[str]) -> bool:
    index = 0
    while index < len(args):
        word = args[index]
        if _is_assignment(word) or word in {"-i", "--ignore-environment", "-0", "--null"}:
            index += 1
            continue
        if word in {"-u", "--unset"}:
            index += 2
            continue
        if word.startswith("-u") and len(word) > 2:
            index += 1
            continue
        return _is_safe_bash_segment(args[index:])
    return True


_GIT_GLOBAL_OPTIONS_WITH_VALUE = {
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
}


def _git_subcommand(args: list[str]) -> tuple[str, list[str]]:
    index = 0
    while index < len(args):
        word = args[index]
        if word in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if any(word.startswith(f"{option}=") for option in _GIT_GLOBAL_OPTIONS_WITH_VALUE if option.startswith("--")):
            index += 1
            continue
        if word == "--":
            index += 1
            continue
        if word.startswith("-"):
            index += 1
            continue
        return word, args[index + 1:]
    return "", []


def _is_read_only_git_config(args: list[str]) -> bool:
    read_flags = {
        "--get", "--get-all", "--get-regexp", "--get-urlmatch",
        "--list", "-l", "--show-origin", "--show-scope",
    }
    if any(arg in read_flags for arg in args):
        return True
    return len(args) == 1 and not args[0].startswith("-")


def _is_read_only_git_ref_command(subcommand: str, args: list[str]) -> bool:
    write_flags = {"-d", "-D", "-m", "-M", "--delete", "--move", "--force"}
    if any(arg in write_flags for arg in args):
        return False
    if subcommand == "tag" and any(arg in {"-l", "--list"} for arg in args):
        return True
    return not any(not arg.startswith("-") for arg in args)


def capability_for_tool(tool: str, args: dict) -> PermissionCapability:
    if tool in {
        "read", "glob", "grep", "webfetch", "websearch", "todo", "task_status",
        "repo_map", "lsp_diagnostics", "lsp_symbols", "lsp_definition",
        "lsp_references",
    }:
        return PermissionCapability.READ_TOOLS
    if tool in {"write", "edit", "apply_patch"}:
        return PermissionCapability.FILE_WRITE
    if tool == "lsp_format":
        return PermissionCapability.FILE_FORMAT
    if tool == "bash":
        return PermissionCapability.BASH_READ if is_safe_bash(str(args.get("command", ""))) else PermissionCapability.BASH_WRITE
    if tool == "git":
        return PermissionCapability.GIT_READ if _is_read_only_git_tool_command(args) else PermissionCapability.GIT_WRITE
    if tool == "agent":
        return PermissionCapability.AGENT_IMPLEMENT if delegated_agent(args) == "implement" else PermissionCapability.AGENT_READONLY
    if tool.startswith("mcp__") or tool.startswith("mcp/"):
        return PermissionCapability.MCP_TOOLS
    return PermissionCapability.OTHER


_FILE_PATTERN_TOOLS = {
    "read", "write", "edit", "apply_patch",
    "lsp_diagnostics", "lsp_symbols", "lsp_definition",
    "lsp_references", "lsp_format",
}


def _is_read_only_git_tool_command(args: dict) -> bool:
    return str(args.get("command", "")) in {
        "status",
        "diff",
        "log",
        "blame",
        "branch_list",
        "remote_list",
    }
