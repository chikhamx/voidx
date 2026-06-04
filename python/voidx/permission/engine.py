"""Central permission engine: capability classification, policies, and mode overlays."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from voidx.config import ApprovalPolicy, ApprovalReviewer, PermissionMode
from voidx.permission.evaluate import evaluate
from voidx.permission.sandbox import check_sandbox_bash, check_sandbox_filepath
from voidx.permission.schema import Action, Rule, Ruleset
from voidx.permission.wildcard import match as wildcard_match


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


BASIC_RULES: Ruleset = [
    Rule(permission="read", pattern="*", action="allow"),
    Rule(permission="glob", pattern="*", action="allow"),
    Rule(permission="grep", pattern="*", action="allow"),
    Rule(permission="webfetch", pattern="*", action="allow"),
    Rule(permission="websearch", pattern="*", action="allow"),
    Rule(permission="todo", pattern="*", action="allow"),
    Rule(permission="task_status", pattern="*", action="allow"),
    Rule(permission="repo_map", pattern="*", action="allow"),
    Rule(permission="lsp_diagnostics", pattern="*", action="allow"),
    Rule(permission="lsp_symbols", pattern="*", action="allow"),
    Rule(permission="lsp_definition", pattern="*", action="allow"),
    Rule(permission="lsp_references", pattern="*", action="allow"),
    Rule(permission="agent", pattern="*", action="allow"),
    Rule(permission="write", pattern="*", action="ask"),
    Rule(permission="edit", pattern="*", action="ask"),
    Rule(permission="bash", pattern="*", action="ask"),
    Rule(permission="lsp_format", pattern="*", action="ask"),
    Rule(permission="agent", pattern="implement", action="ask"),
    Rule(permission="mcp__*", pattern="*", action="ask"),
    Rule(permission="mcp/*", pattern="*", action="ask"),
]


@dataclass(frozen=True)
class PermissionContext:
    workspace: str
    interaction_mode: str = "auto"
    permission_mode: str = PermissionMode.DEFAULT.value
    sandbox_mode: str = "workspace-write"
    sandbox_workspace_write: tuple[str, ...] = ()
    approval_policy: str = ApprovalPolicy.UNTRUSTED.value
    approval_reviewer: str = ApprovalReviewer.USER.value
    session_allow: frozenset[str] = field(default_factory=frozenset)
    session_deny: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_service(
        cls,
        service,
        *,
        workspace: str,
        interaction_mode: str | None = None,
        plan_mode: bool = False,
    ) -> "PermissionContext":
        mode = interaction_mode or "auto"
        if plan_mode:
            mode = "plan"
        return cls(
            workspace=workspace,
            interaction_mode=mode,
            permission_mode=getattr(service, "permission_mode", PermissionMode.DEFAULT.value),
            sandbox_mode=getattr(service, "sandbox_mode", "workspace-write"),
            sandbox_workspace_write=tuple(getattr(service, "sandbox_workspace_write", []) or []),
            approval_policy=getattr(service, "approval_policy", ApprovalPolicy.UNTRUSTED.value),
            approval_reviewer=getattr(service, "approval_reviewer", ApprovalReviewer.USER.value),
            session_allow=frozenset(getattr(service, "_session_allow", set())),
            session_deny=frozenset(getattr(service, "_session_deny", set())),
        )


@dataclass(frozen=True)
class ClassifiedToolCall:
    tool_call: dict
    name: str
    args: dict
    pattern: str
    capability: PermissionCapability


@dataclass(frozen=True)
class PermissionDecision:
    action: Action
    tool_call: dict
    name: str
    args: dict
    pattern: str
    capability: PermissionCapability
    source: str
    reason: str = ""
    failure_check: bool = False


def authorize_tool_call(tool_call: dict, context: PermissionContext) -> PermissionDecision:
    classified = classify_tool_call(tool_call)

    reason = sandbox_denial_reason(classified, context)
    if reason:
        return _decision(classified, "deny", "sandbox", reason)

    reason = mode_overlay_denial_reason(classified, context)
    if reason:
        return _decision(classified, "deny", "mode", reason)

    session_action = session_action_for_tool(classified.name, context)
    if session_action:
        reason = _reason_for(classified, session_action)
        return _decision(classified, session_action, "session", reason)

    action = strategy_action_for_tool(classified, context)
    if action != "ask":
        return _decision(classified, action, "strategy", _reason_for(classified, action))

    return resolve_approval(classified, context)


def decide_base_action(tool: str, pattern: str, context: PermissionContext) -> Action:
    classified = classify_tool_call(tool_call_from_pattern(tool, pattern))
    session_action = session_action_for_tool(classified.name, context)
    if session_action:
        return session_action
    return strategy_action_for_tool(classified, context)


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
        capability=_capability(name, args),
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


def sandbox_denial_reason(classified: ClassifiedToolCall, context: PermissionContext) -> str | None:
    if context.sandbox_mode == "danger-full-access":
        return None

    if context.sandbox_mode == "read-only":
        if classified.capability in {
            PermissionCapability.FILE_WRITE,
            PermissionCapability.FILE_FORMAT,
            PermissionCapability.BASH_WRITE,
        }:
            return f"SANDBOX READ-ONLY: '{classified.name}' is not allowed."
        if classified.capability == PermissionCapability.AGENT_IMPLEMENT:
            return "SANDBOX READ-ONLY: cannot delegate to implement."
        return None

    if context.sandbox_mode == "workspace-write":
        if classified.capability in {PermissionCapability.FILE_WRITE, PermissionCapability.FILE_FORMAT}:
            file_path = classified.args.get("file_path", "")
            if file_path:
                return check_sandbox_filepath(
                    file_path,
                    context.workspace,
                    list(context.sandbox_workspace_write),
                )
        if classified.name == "bash":
            command = classified.args.get("command", "")
            if command:
                return check_sandbox_bash(
                    command,
                    context.workspace,
                    list(context.sandbox_workspace_write),
                )
    return None


def mode_overlay_denial_reason(classified: ClassifiedToolCall, context: PermissionContext) -> str | None:
    if context.interaction_mode != "plan":
        return None
    if classified.capability in {
        PermissionCapability.FILE_WRITE,
        PermissionCapability.FILE_FORMAT,
        PermissionCapability.BASH_WRITE,
    }:
        return f"BLOCKED by plan mode: '{classified.name}' is not allowed."
    if classified.capability == PermissionCapability.AGENT_IMPLEMENT:
        return "BLOCKED by plan mode: cannot delegate to implement."
    return None


def session_action_for_tool(tool: str, context: PermissionContext) -> Action | None:
    if any(_session_rule_matches(tool, rule) for rule in context.session_deny):
        return "deny"
    if any(_session_rule_matches(tool, rule) for rule in context.session_allow):
        return "allow"
    return None


def strategy_action_for_tool(classified: ClassifiedToolCall, context: PermissionContext) -> Action:
    if context.permission_mode == PermissionMode.ACCEPT_EDITS.value and classified.capability in {
        PermissionCapability.FILE_WRITE,
        PermissionCapability.FILE_FORMAT,
    }:
        return "allow"
    if classified.capability == PermissionCapability.BASH_READ:
        return "allow"
    return evaluate(classified.name, classified.pattern, BASIC_RULES).action


def resolve_approval(classified: ClassifiedToolCall, context: PermissionContext) -> PermissionDecision:
    policy = context.approval_policy
    if policy in {ApprovalPolicy.NEVER.value, ApprovalPolicy.ON_REQUEST.value}:
        return _decision(classified, "allow", "approval_policy", _reason_for(classified, "allow"))

    if policy == ApprovalPolicy.ON_FAILURE.value:
        if classified.capability == PermissionCapability.BASH_WRITE:
            return _decision(classified, "ask", "approval_policy", _reason_for(classified, "ask"))
        return _decision(
            classified,
            "allow",
            "approval_policy",
            _reason_for(classified, "allow"),
            failure_check=True,
        )

    if context.approval_reviewer == ApprovalReviewer.AUTO_REVIEW.value:
        if classified.capability == PermissionCapability.BASH_WRITE:
            return _decision(classified, "ask", "auto_review", _reason_for(classified, "ask"))
        return _decision(classified, "allow", "auto_review", _reason_for(classified, "allow"))

    return _decision(classified, "ask", "strategy", _reason_for(classified, "ask"))


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
    return "*"


def delegated_agent(args: dict) -> str:
    return str(args.get("agent") or "")


def is_safe_bash(command: str) -> bool:
    stripped = command.strip()
    if not stripped or stripped.startswith("#"):
        return True
    if re.search(r" > ", stripped) or re.search(r" >> ", stripped):
        return False
    if re.search(r"\|\s*tee\b", stripped):
        return False

    words = stripped.split()
    prog = words[0].lower()

    if prog == "git" and len(words) > 1:
        sub = words[1]
        read_only_git = {
            "status", "log", "diff", "show", "blame", "rev-parse", "rev-list",
            "ls-files", "ls-tree", "describe", "shortlog", "reflog", "cherry",
            "whatchanged", "notes", "grep", "bisect",
            "config", "stash", "branch", "tag", "remote", "worktree",
        }
        if sub not in read_only_git:
            return False
        if sub == "stash":
            return len(words) > 2 and words[2] in ("list", "show")
        if sub == "bisect":
            return len(words) > 2 and words[2] in ("log", "view", "visualize")
        if sub in ("branch", "tag"):
            return "-d" not in words and "-D" not in words
        if sub == "remote":
            return "-v" in words or "--verbose" in words or len(words) == 2
        if sub == "worktree":
            return len(words) > 2 and words[2] == "list"
        return True

    if prog == "gh" and len(words) > 1:
        sub = words[1]
        if sub == "pr":
            return len(words) > 2 and words[2] in ("view", "list", "status", "checks", "diff")
        if sub == "issue":
            return len(words) > 2 and words[2] in ("view", "list", "status")
        if sub == "api":
            cmd_upper = stripped.upper()
            if "-X" in cmd_upper or "--METHOD" in cmd_upper:
                return "GET" in cmd_upper
            return True
        if sub in ("auth", "config", "completion", "secret"):
            return len(words) == 2 or (len(words) > 2 and words[2] in ("list", "status", "view"))
        return False

    read_only = {
        "ls", "dir", "cat", "head", "tail", "wc", "which", "where", "whereis",
        "echo", "printf", "pwd", "date", "whoami", "uname", "env", "printenv",
        "df", "du", "sort", "uniq", "cut", "tr", "column", "less", "more",
        "find", "grep", "egrep", "fgrep", "rg", "file", "stat", "od",
        "true", "false", "test", "[", "type", "basename", "dirname",
        "realpath", "readlink", "hostname", "id", "groups", "logname",
        "uptime", "free", "swapon", "lscpu", "lsblk", "lspci", "lsusb",
    }
    if prog in read_only:
        return True

    if prog in ("pip", "pip3") and len(words) > 1:
        return words[1] in ("list", "show", "freeze", "config", "cache")
    if prog in ("npm", "npx") and len(words) > 1:
        return words[1] in ("list", "ls", "view", "info", "outdated")
    if prog == "cargo" and len(words) > 1:
        return words[1] in ("search", "doc", "readme")
    if prog == "go" and len(words) > 1:
        return words[1] in ("list", "doc", "version", "env")

    return False


def _capability(tool: str, args: dict) -> PermissionCapability:
    if tool in {
        "read", "glob", "grep", "webfetch", "websearch", "todo", "task_status",
        "repo_map", "lsp_diagnostics", "lsp_symbols", "lsp_definition",
        "lsp_references",
    }:
        return PermissionCapability.READ_TOOLS
    if tool in {"write", "edit"}:
        return PermissionCapability.FILE_WRITE
    if tool == "lsp_format":
        return PermissionCapability.FILE_FORMAT
    if tool == "bash":
        return PermissionCapability.BASH_READ if is_safe_bash(str(args.get("command", ""))) else PermissionCapability.BASH_WRITE
    if tool == "agent":
        return PermissionCapability.AGENT_IMPLEMENT if delegated_agent(args) == "implement" else PermissionCapability.AGENT_READONLY
    if tool.startswith("mcp__") or tool.startswith("mcp/"):
        return PermissionCapability.MCP_TOOLS
    return PermissionCapability.OTHER


def _session_rule_matches(tool: str, rule: str) -> bool:
    if wildcard_match(tool, rule):
        return True
    if rule.startswith("mcp/"):
        return wildcard_match(tool, rule.replace("/", "__"))
    return False


def _decision(
    classified: ClassifiedToolCall,
    action: Action,
    source: str,
    reason: str = "",
    *,
    failure_check: bool = False,
) -> PermissionDecision:
    return PermissionDecision(
        action=action,
        tool_call=classified.tool_call,
        name=classified.name,
        args=classified.args,
        pattern=classified.pattern,
        capability=classified.capability,
        source=source,
        reason=reason,
        failure_check=failure_check,
    )


def _reason_for(classified: ClassifiedToolCall, action: Action) -> str:
    if action == "deny":
        return f"Permission denied: {classified.name} → {classified.pattern}"
    if action == "allow":
        return f"Permission allowed: {classified.name} → {classified.pattern}"
    return f"Permission required: {classified.name} → {classified.pattern}"


_FILE_PATTERN_TOOLS = {
    "read", "write", "edit",
    "lsp_diagnostics", "lsp_symbols", "lsp_definition",
    "lsp_references", "lsp_format",
}
