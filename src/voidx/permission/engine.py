"""Central permission engine: authorization flow and mode overlays."""

from __future__ import annotations

from voidx.config import PermissionMode
from voidx.permission.context import PermissionContext, PermissionDecision
from voidx.permission.evaluate import evaluate
from voidx.permission.git_policy import git_sandbox_precheck
from voidx.permission.shell_policy import classify_shell_risk, shell_sandbox_precheck
from voidx.permission.grants import resolve_access
from voidx.permission.presets import resolve_mode_decision
from voidx.permission.risk import RiskAssessment, RiskLevel, RiskTag
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

    sandbox_action, reason = sandbox_precheck_action(classified, context)
    if sandbox_action == "deny":
        if classified.name in {"bash", "powershell"}:
            risk = _risk_for(classified, "ask", reason or "")
            if risk.level == RiskLevel.BLOCKED:
                return _decision(classified, "ask", "sandbox", risk.reason, context=context)
        return _decision(classified, "deny", "sandbox", reason or "", context=context)

    if sandbox_action == "defer":
        return _decision(classified, "ask", "sandbox", reason or _reason_for(classified, "ask"), context=context)

    session_action = session_action_for_tool(classified.name, context)
    if session_action:
        reason = _reason_for(classified, session_action)
        return _decision(classified, session_action, "session", reason, context=context)

    base_action = strategy_action_for_tool(classified, context)
    return _decision(classified, base_action, "preset", reason or _reason_for(classified, base_action), context=context)


def decide_base_action(tool: str, pattern: str, context: PermissionContext) -> Action:
    classified = classify_tool_call(tool_call_from_pattern(tool, pattern))
    session_action = session_action_for_tool(classified.name, context)
    if session_action:
        return session_action
    return _preset_decision_for(_risk_for(classified, "ask", _reason_for(classified, "ask")), context).action


def sandbox_denial_reason(classified: ClassifiedToolCall, context: PermissionContext) -> str | None:
    action, reason = sandbox_precheck_action(classified, context)
    return reason if action == "deny" else None


def sandbox_precheck_action(classified: ClassifiedToolCall, context: PermissionContext) -> tuple[Action, str | None]:
    if not context.permission_state_ready:
        return "deny", "Permission state not ready."
    if context.sandbox_mode == "danger-full-access":
        return "allow", None

    if context.interaction_mode == "plan":
        if classified.name == "bash":
            if classified.capability == PermissionCapability.BASH_WRITE:
                return "deny", f"SANDBOX READ-ONLY: '{classified.name}' is not allowed."
            return shell_sandbox_precheck(classified.args, context, shell="bash")
        if classified.name == "powershell":
            if classified.capability == PermissionCapability.BASH_WRITE:
                return "deny", f"SANDBOX READ-ONLY: '{classified.name}' is not allowed."
            return shell_sandbox_precheck(classified.args, context, shell="powershell")
        if classified.capability in {
            PermissionCapability.FILE_WRITE,
            PermissionCapability.FILE_FORMAT,
            PermissionCapability.BASH_WRITE,
            PermissionCapability.GIT_WRITE,
        }:
            return "deny", f"SANDBOX READ-ONLY: '{classified.name}' is not allowed."
        if classified.capability == PermissionCapability.AGENT_IMPLEMENT:
            return "deny", "SANDBOX READ-ONLY: cannot delegate to implement."
        return "allow", None

    if context.sandbox_mode == "read-only":
        if classified.name == "bash":
            return shell_sandbox_precheck(classified.args, context, shell="bash")
        if classified.name == "powershell":
            return shell_sandbox_precheck(classified.args, context, shell="powershell")
        if classified.capability in {
            PermissionCapability.FILE_WRITE,
            PermissionCapability.FILE_FORMAT,
            PermissionCapability.BASH_WRITE,
            PermissionCapability.GIT_WRITE,
        }:
            return "defer", f"READ ONLY requires approval for '{classified.name}'."
        if classified.capability == PermissionCapability.AGENT_IMPLEMENT:
            return "defer", "READ ONLY requires approval for implement delegation."
        return "allow", None

    if context.sandbox_mode == "workspace-write":
        writable_paths = [
            *context.sandbox_writable_files,
            *context.sandbox_writable_dirs,
        ]
        if classified.name == "git":
            return git_sandbox_precheck(classified.args, context)
        if classified.name in {"read", "write", "replace"}:
            for file_path in file_paths_for_tool(classified.name, classified.args):
                access = "read" if classified.name == "read" else "write"
                resolution = resolve_access(
                    context.workspace,
                    file_path,
                    access=access,
                    access_grants=context.access_grants,
                    require_exists=classified.name == "read",
                    allow_missing_write_file=classified.name in {"write", "replace"},
                )
                if resolution.action == "deny":
                    return "deny", resolution.reason
                if resolution.action == "defer":
                    return "defer", resolution.reason
            return "allow", None
        if classified.capability in {PermissionCapability.FILE_WRITE, PermissionCapability.FILE_FORMAT}:
            for file_path in file_paths_for_tool(classified.name, classified.args):
                reason = check_sandbox_filepath(
                    file_path,
                    context.workspace,
                    writable_paths,
                )
                if reason:
                    return "defer", reason
        if classified.name == "bash":
            return shell_sandbox_precheck(classified.args, context, shell="bash")
        if classified.name == "powershell":
            return shell_sandbox_precheck(classified.args, context, shell="powershell")
    return "allow", None


def session_action_for_tool(tool: str, context: PermissionContext) -> Action | None:
    if any(_session_rule_matches(tool, rule) for rule in context.session_deny):
        return "deny"
    if any(_session_rule_matches(tool, rule) for rule in context.session_allow):
        return "allow"
    return None


def strategy_action_for_tool(classified: ClassifiedToolCall, context: PermissionContext) -> Action:
    if classified.capability in {PermissionCapability.BASH_READ, PermissionCapability.GIT_READ}:
        return "allow"
    permission = "edit" if classified.name in {"manage", "write", "replace"} else classified.name
    return evaluate(permission, classified.pattern, BASIC_RULES).action


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
    context: PermissionContext | None = None,
) -> PermissionDecision:
    risk = _risk_for(classified, action, reason)
    allowed_scopes = ()
    default_scope = None
    if action == "ask":
        preset_decision = _preset_decision_for(risk, context)
        action = preset_decision.action
        if action == "blocked_ack":
            reason = risk.reason
        elif action == "ask":
            allowed_scopes = preset_decision.allowed_scopes
            default_scope = preset_decision.default_scope
        else:
            reason = risk.reason or reason
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
        risk=risk,
        allowed_scopes=allowed_scopes,
        default_scope=default_scope,
    )



def _preset_decision_for(risk: RiskAssessment, context: PermissionContext | None):
    raw = context.permission_mode if context else PermissionMode.SAFE.value
    try:
        preset = PermissionMode(raw)
    except ValueError:
        preset = PermissionMode.SAFE
    return resolve_mode_decision(preset, risk)

def _risk_for(classified: ClassifiedToolCall, action: Action, reason: str) -> RiskAssessment:
    if classified.name == "bash":
        return classify_shell_risk(str(classified.args.get("command") or ""), shell="bash")
    if classified.name == "powershell":
        return classify_shell_risk(str(classified.args.get("command") or ""), shell="powershell")
    if action == "allow":
        return RiskAssessment.normal(tool_name=classified.name, pattern=classified.pattern, tags=(RiskTag.SAFE_READ,), reason=reason)
    if action == "deny":
        return RiskAssessment.blocked(tool_name=classified.name, pattern=classified.pattern, tags=(), reason=reason)
    tags = (RiskTag.WORKSPACE_EDIT,) if classified.capability in {PermissionCapability.FILE_WRITE, PermissionCapability.FILE_FORMAT} else ()
    return RiskAssessment.dangerous(tool_name=classified.name, pattern=classified.pattern, tags=tags, reason=reason)


def _reason_for(classified: ClassifiedToolCall, action: Action) -> str:
    if action == "deny":
        return f"Permission denied: {classified.name} → {classified.pattern}"
    if action == "allow":
        return f"Permission allowed: {classified.name} → {classified.pattern}"
    return f"Permission required: {classified.name} → {classified.pattern}"
