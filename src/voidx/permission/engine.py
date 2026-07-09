"""Central permission engine: authorization flow and mode overlays."""

from __future__ import annotations

from voidx.config import ApprovalPolicy, ApprovalReviewer, PermissionMode
from voidx.permission.context import PermissionContext, PermissionDecision
from voidx.permission.evaluate import evaluate
from voidx.permission.rules import (
    BASIC_RULES,
    ClassifiedToolCall,
    PermissionCapability,
    build_pattern,
    classify_tool_call,
    delegated_agent,
    file_paths_for_tool,
    is_safe_bash,
    repair_tool_name,
    tool_call_from_pattern,
)
from voidx.permission.sandbox import check_sandbox_bash, check_sandbox_filepath
from voidx.tools.powershell.sandbox import check_sandbox_powershell
from voidx.permission.schema import Action
from voidx.permission.wildcard import match as wildcard_match


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


def sandbox_denial_reason(classified: ClassifiedToolCall, context: PermissionContext) -> str | None:
    if context.sandbox_mode == "danger-full-access":
        return None

    if context.sandbox_mode == "read-only":
        if classified.capability in {
            PermissionCapability.FILE_WRITE,
            PermissionCapability.FILE_FORMAT,
            PermissionCapability.BASH_WRITE,
            PermissionCapability.GIT_WRITE,
        }:
            return f"SANDBOX READ-ONLY: '{classified.name}' is not allowed."
        if classified.capability == PermissionCapability.AGENT_IMPLEMENT:
            return "SANDBOX READ-ONLY: cannot delegate to implement."
        return None

    if context.sandbox_mode == "workspace-write":
        if classified.capability in {PermissionCapability.FILE_WRITE, PermissionCapability.FILE_FORMAT}:
            for file_path in file_paths_for_tool(classified.name, classified.args):
                reason = check_sandbox_filepath(
                    file_path,
                    context.workspace,
                    list(context.sandbox_workspace_write),
                )
                if reason:
                    return reason
        if classified.name == "bash":
            command = classified.args.get("command", "")
            if command:
                return check_sandbox_bash(
                    command,
                    context.workspace,
                    list(context.sandbox_workspace_write),
                )
        if classified.name == "powershell":
            command = classified.args.get("command", "")
            if command:
                return check_sandbox_powershell(
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
        PermissionCapability.GIT_WRITE,
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
    if classified.capability in {PermissionCapability.BASH_READ, PermissionCapability.GIT_READ}:
        return "allow"
    permission = "edit" if classified.name in {"manage", "write", "replace"} else classified.name
    return evaluate(permission, classified.pattern, BASIC_RULES).action


def resolve_approval(classified: ClassifiedToolCall, context: PermissionContext) -> PermissionDecision:
    policy = context.approval_policy
    if policy in {ApprovalPolicy.NEVER.value, ApprovalPolicy.ON_REQUEST.value}:
        return _decision(classified, "allow", "approval_policy", _reason_for(classified, "allow"))

    if policy == ApprovalPolicy.ON_FAILURE.value:
        if classified.capability in {PermissionCapability.BASH_WRITE, PermissionCapability.GIT_WRITE}:
            return _decision(classified, "ask", "approval_policy", _reason_for(classified, "ask"))
        return _decision(
            classified,
            "allow",
            "approval_policy",
            _reason_for(classified, "allow"),
            failure_check=True,
        )

    if context.approval_reviewer == ApprovalReviewer.AUTO_REVIEW.value:
        if classified.capability in {PermissionCapability.BASH_WRITE, PermissionCapability.GIT_WRITE}:
            return _decision(classified, "ask", "auto_review", _reason_for(classified, "ask"))
        return _decision(classified, "allow", "auto_review", _reason_for(classified, "allow"))

    return _decision(classified, "ask", "strategy", _reason_for(classified, "ask"))


def _session_rule_matches(tool: str, rule: str) -> bool:
    if rule == "edit" and tool in {"manage", "write", "replace"}:
        return True
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
