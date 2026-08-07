"""Session-scoped permission rules for repeated approvals."""

from __future__ import annotations

import shlex

from voidx.tooling.domain.authorization import PermissionDecision
from voidx.tooling.domain.risk import RiskLevel, RiskTag
from voidx.tooling.policy.permission.rules import ClassifiedToolCall
from voidx.tooling.policy.permission.wildcard import match as wildcard_match

_RULE_MARKER = "@"
_SCOPE_SEPARATOR = ":"
_PATTERN_SCOPE = "pattern"
_TARGET_SCOPE = "target"


def scoped_session_rule_for_decision(decision: PermissionDecision) -> str:
    scope, value = _session_scope_for_decision(decision)
    return f"{decision.name}{_RULE_MARKER}{scope}{_SCOPE_SEPARATOR}{value}"


def session_rule_matches(classified: ClassifiedToolCall, rule: str) -> bool:
    parsed = _parse_scoped_rule(rule)
    if parsed is None:
        return _tool_rule_matches(classified.name, rule)
    tool_rule, scope, value = parsed
    if not _tool_rule_matches(classified.name, tool_rule):
        return False
    if scope == _PATTERN_SCOPE:
        return wildcard_match(classified.pattern, value)
    if scope == _TARGET_SCOPE:
        return _shell_command_target(classified.name, str(classified.args.get("command") or "")) == value
    return False


def format_session_rule(rule: str) -> str:
    parsed = _parse_scoped_rule(rule)
    if parsed is None:
        return rule
    tool, scope, value = parsed
    return f"{tool}({scope}: {value})"


def _session_scope_for_decision(decision: PermissionDecision) -> tuple[str, str]:
    if _is_target_scoped_shell_decision(decision):
        target = _shell_command_target(decision.name, str(decision.args.get("command") or ""))
        if target:
            return _TARGET_SCOPE, target
    return _PATTERN_SCOPE, decision.pattern


def _is_target_scoped_shell_decision(decision: PermissionDecision) -> bool:
    if decision.name not in {"bash", "powershell"} or decision.risk is None:
        return False
    if decision.risk.level != RiskLevel.DANGEROUS:
        return False
    if RiskTag.WORKSPACE_EDIT not in decision.risk.tags:
        return False
    return decision.risk.reason in {"unknown shell command", "unknown powershell command"}


def _shell_command_target(tool: str, command: str) -> str:
    try:
        words = shlex.split(command.strip(), posix=tool != "powershell")
    except ValueError:
        return ""
    return words[0] if words else ""


def _parse_scoped_rule(rule: str) -> tuple[str, str, str] | None:
    if _RULE_MARKER not in rule:
        return None
    tool, scope_value = rule.split(_RULE_MARKER, 1)
    if _SCOPE_SEPARATOR not in scope_value:
        return None
    scope, value = scope_value.split(_SCOPE_SEPARATOR, 1)
    if not tool or not value:
        return None
    return tool, scope, value


def _tool_rule_matches(tool: str, rule: str) -> bool:
    if rule == "mcp" and tool.startswith("mcp__"):
        return True
    if rule == "edit" and tool in {"manage", "write", "replace"}:
        return True
    if wildcard_match(tool, rule):
        return True
    return False
