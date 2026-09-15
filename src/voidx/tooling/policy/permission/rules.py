"""Permission rules and tool capability classification."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import Enum

from voidx.tooling.domain.permission import Rule, Ruleset
from voidx.tooling.domain.tool_names import canonical_tool_name
from voidx.tooling.policy.filesystem.constants import FILE_PATTERN_TOOLS
from voidx.tooling.policy.shell.policy import shell_policy_for_command


class PermissionCapability(str, Enum):
    READ_TOOLS = "read_tools"
    FILE_WRITE = "file_write"
    FILE_FORMAT = "file_format"
    BASH_READ = "bash_read"
    BASH_WRITE = "bash_write"
    AGENT_READONLY = "agent_readonly"
    AGENT_IMPLEMENT = "agent_implement"
    MCP_TOOLS = "mcp_tools"
    OTHER = "other"


ALWAYS_ALLOWED_TOOLS = frozenset({"agent", "agent_control", "mcp", "skill"})


def is_always_allowed_tool(tool: str) -> bool:
    return tool in ALWAYS_ALLOWED_TOOLS or tool.startswith("mcp__")


BASIC_RULES: Ruleset = [
    Rule(permission="read", pattern="*", action="allow"),
    Rule(permission="find", pattern="*", action="allow"),
    Rule(permission="search", pattern="*", action="allow"),
    Rule(permission="webfetch", pattern="*", action="allow"),
    Rule(permission="websearch", pattern="*", action="allow"),
    Rule(permission="todo", pattern="*", action="allow"),
    Rule(permission="clarify", pattern="*", action="allow"),
    Rule(permission="checkpoint", pattern="*", action="allow"),
    Rule(permission="workflow", pattern="*", action="allow"),
    Rule(permission="compact", pattern="*", action="allow"),
    Rule(permission="document", pattern="*", action="allow"),
    Rule(permission="lsp", pattern="*", action="allow"),
    Rule(permission="agent", pattern="*", action="allow"),
    Rule(permission="agent_control", pattern="*", action="allow"),
    Rule(permission="mcp", pattern="*", action="allow"),
    Rule(permission="skill", pattern="*", action="allow"),
    Rule(permission="edit", pattern="*", action="ask"),
    Rule(permission="bash", pattern="*", action="ask"),
    Rule(permission="powershell", pattern="*", action="ask"),
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
    elif name == "powershell":
        args = {"command": pattern}
    elif name == "agent":
        args = {"name": pattern}
    elif name == "manage":
        args = {"op": "create", "paths": pattern}
    elif name in FILE_PATTERN_TOOLS:
        args = {"file_path": pattern}
    else:
        args = {}
    return {"name": name, "args": args}


def repair_tool_name(tool: str) -> str:
    return canonical_tool_name(tool)


def build_pattern(tool: str, args: dict) -> str:
    if tool == "bash" or tool == "powershell":
        return str(args.get("command", "*"))
    paths = file_paths_for_tool(tool, args)
    if paths:
        return paths[0] if len(paths) == 1 else " | ".join(paths)
    if tool == "agent":
        return "implement" if args.get("invocation_class") == "implement" else "voidx"
    if tool == "skill":
        return "create" if args.get("op") == "create" else "*"
    if tool == "mcp":
        return _mcp_gateway_pattern(args)
    return "*"


def _mcp_gateway_pattern(args: dict) -> str:
    op = str(args.get("op") or "")
    if op in {"list", "load"}:
        return op
    server = str(args.get("server") or "*")
    tool_name = str(args.get("tool") or "*")
    return f"mcp:{server}:{tool_name}"




def is_safe_bash(command: str) -> bool:
    decision = shell_policy_for_command(command, shell="bash")
    return decision.allowed and decision.read_only


def shell_words(command: str) -> list[str] | None:
    try:
        lexer = shlex.shlex(command, posix=False, punctuation_chars=True)
        lexer.whitespace_split = True
        return [_strip_quotes(word) for word in lexer]
    except ValueError:
        return None


def _strip_quotes(word: str) -> str:
    """Strip one layer of surrounding single or double quotes (posix=False compat)."""
    if len(word) >= 2 and word[0] == word[-1] and word[0] in ("'", '"'):
        return word[1:-1]
    return word


def capability_for_tool(tool: str, args: dict) -> PermissionCapability:
    if tool in {
        "read", "find", "search", "webfetch", "websearch", "todo",
        "document",
        "workflow", "compact",
        "lsp",
    }:
        return PermissionCapability.READ_TOOLS
    if tool == "skill":
        if args.get("op") == "create":
            return PermissionCapability.FILE_WRITE
        return PermissionCapability.READ_TOOLS
    if tool in {"manage", "write", "replace"}:
        return PermissionCapability.FILE_WRITE
    if tool == "lsp_format":
        return PermissionCapability.FILE_FORMAT
    if tool == "bash":
        return PermissionCapability.BASH_READ if is_safe_bash(str(args.get("command", ""))) else PermissionCapability.BASH_WRITE
    if tool == "powershell":
        from voidx.tooling.policy.shell.powershell_sandbox import is_safe_powershell_command
        return PermissionCapability.BASH_READ if is_safe_powershell_command(str(args.get("command", ""))) else PermissionCapability.BASH_WRITE
    if tool == "agent":
        return (
            PermissionCapability.AGENT_IMPLEMENT
            if args.get("invocation_class") == "implement"
            else PermissionCapability.AGENT_READONLY
        )
    if tool == "mcp":
        if str(args.get("op") or "") == "call":
            return PermissionCapability.MCP_TOOLS
        return PermissionCapability.READ_TOOLS
    return PermissionCapability.OTHER


def file_paths_for_tool(tool: str, args: dict) -> list[str]:
    if tool in FILE_PATTERN_TOOLS:
        file_path = args.get("file_path")
        return [str(file_path)] if file_path else []
    if tool in {"search", "find"}:
        path = args.get("path")
        return [str(path)] if path else []
    if tool != "manage":
        return []

    op = str(args.get("op") or "")
    if op in {"create", "delete"}:
        paths = args.get("paths")
        if isinstance(paths, list):
            return [str(path) for path in paths if path]
        return [str(paths)] if paths else []
    if op == "move":
        paths: list[str] = []
        moves = args.get("moves")
        if isinstance(moves, list):
            for move in moves:
                if not isinstance(move, dict):
                    continue
                for key in ("src", "dest"):
                    value = move.get(key)
                    if value:
                        paths.append(str(value))
        return paths
    return []
