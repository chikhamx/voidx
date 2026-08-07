"""Central permission engine: authorization flow and mode overlays."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from voidx.tooling.domain.interaction import UserInteraction
from voidx.tooling.domain.result import ToolResult

from voidx.tooling.domain.permission import PermissionMode
from voidx.tooling.domain.authorization import PermissionContext, PermissionDecision
from voidx.tooling.policy.permission.evaluate import evaluate
from voidx.tooling.policy.git.policy import git_policy_for_args, git_sandbox_precheck
from voidx.tooling.policy.shell.policy import classify_shell_risk, shell_sandbox_precheck
from voidx.tooling.domain.grants import AccessGrant, AccessGrants, AccessIntent, ApprovalPrecondition, GrantPersistence, ObjectType
from voidx.tooling.policy.filesystem.grants import grant_for_intent, resolve_access
from voidx.tooling.policy.permission.presets import resolve_mode_decision
from voidx.tooling.domain.risk import RiskAssessment, RiskLevel, RiskTag
from voidx.tooling.policy.permission.rules import (
    BASIC_RULES,
    ClassifiedToolCall,
    PermissionCapability,
    build_pattern,
    classify_tool_call,
    file_paths_for_tool,
    is_always_allowed_tool,
    is_safe_bash,
    repair_tool_name,
    tool_call_from_pattern,
)
from voidx.tooling.policy.filesystem.sandbox import check_sandbox_bash, check_sandbox_filepath
from voidx.tooling.policy.permission.session_rules import session_rule_matches
from voidx.tooling.policy.shell.powershell_sandbox import check_sandbox_powershell
from voidx.tooling.domain.permission import Action


def authorize_tool_call(tool_call: dict, context: PermissionContext) -> PermissionDecision:
    classified = classify_tool_call(tool_call)
    session_action = session_action_for_tool(classified, context)

    if not context.permission_state_ready:
        return _decision(classified, "deny", "sandbox", "Permission state not ready.", context=context)
    if session_action == "deny":
        return _decision(classified, "deny", "session", _reason_for(classified, "deny"), context=context)
    if is_always_allowed_tool(classified.name):
        return _decision(classified, "allow", "preset", _reason_for(classified, "allow"), context=context)

    sandbox_action, reason, access_intents = sandbox_precheck_action(classified, context)
    if sandbox_action == "deny":
        if classified.name in {"bash", "powershell"}:
            risk = _risk_for(classified, "ask", reason or "")
            if risk.level == RiskLevel.BLOCKED:
                return _decision(classified, "ask", "sandbox", risk.reason, context=context)
        return _decision(classified, "deny", "sandbox", reason or "", context=context)

    if sandbox_action == "defer":
        if session_action == "deny":
            return _decision(classified, "deny", "session", _reason_for(classified, "deny"), context=context)
        if session_action == "allow" and context.sandbox_mode == "workspace-write":
            return _decision(classified, "allow", "session", _reason_for(classified, "allow"), context=context)
        return _decision(classified, "ask", "sandbox", reason or _reason_for(classified, "ask"), context=context, access_intents=access_intents)

    if session_action:
        reason = _reason_for(classified, session_action)
        return _decision(classified, session_action, "session", reason, context=context)

    base_action = strategy_action_for_tool(classified, context)
    return _decision(classified, base_action, "preset", reason or _reason_for(classified, base_action), context=context)


def decide_base_action(tool: str, pattern: str, context: PermissionContext) -> Action:
    classified = classify_tool_call(tool_call_from_pattern(tool, pattern))
    session_action = session_action_for_tool(classified, context)
    if session_action:
        return session_action
    return _preset_decision_for(_risk_for(classified, "ask", _reason_for(classified, "ask")), context).action


def sandbox_denial_reason(classified: ClassifiedToolCall, context: PermissionContext) -> str | None:
    action, reason, _intents = sandbox_precheck_action(classified, context)
    return reason if action == "deny" else None


def sandbox_precheck_action(classified: ClassifiedToolCall, context: PermissionContext) -> tuple[Action, str | None, tuple[AccessIntent, ...]]:
    if not context.permission_state_ready:
        return "deny", "Permission state not ready.", ()
    if context.sandbox_mode == "danger-full-access":
        return "allow", None, ()

    if context.interaction_mode == "plan":
        if classified.name == "bash":
            if classified.capability == PermissionCapability.BASH_WRITE:
                return "deny", f"SANDBOX READ-ONLY: '{classified.name}' is not allowed.", ()
            return (*shell_sandbox_precheck(classified.args, context, shell="bash"), ())
        if classified.name == "powershell":
            if classified.capability == PermissionCapability.BASH_WRITE:
                return "deny", f"SANDBOX READ-ONLY: '{classified.name}' is not allowed.", ()
            return (*shell_sandbox_precheck(classified.args, context, shell="powershell"), ())
        if classified.capability in {
            PermissionCapability.FILE_WRITE,
            PermissionCapability.FILE_FORMAT,
            PermissionCapability.BASH_WRITE,
            PermissionCapability.GIT_WRITE,
        }:
            return "deny", f"SANDBOX READ-ONLY: '{classified.name}' is not allowed.", ()
        return "allow", None, ()

    if context.sandbox_mode == "read-only":
        if classified.name == "bash":
            return (*shell_sandbox_precheck(classified.args, context, shell="bash"), ())
        if classified.name == "powershell":
            return (*shell_sandbox_precheck(classified.args, context, shell="powershell"), ())
        if classified.capability in {
            PermissionCapability.FILE_WRITE,
            PermissionCapability.FILE_FORMAT,
            PermissionCapability.BASH_WRITE,
            PermissionCapability.GIT_WRITE,
        }:
            return "defer", f"READ ONLY requires approval for '{classified.name}'.", ()
        return "allow", None, ()

    if context.sandbox_mode == "workspace-write":
        writable_paths = [
            *context.sandbox_writable_files,
            *context.sandbox_writable_dirs,
        ]
        if classified.name == "git":
            return git_sandbox_precheck(classified.args, context)
        path_tool_names = {"read", "write", "replace", "manage", "lsp_format", "lsp"}
        if classified.name in path_tool_names or classified.capability in {PermissionCapability.FILE_WRITE, PermissionCapability.FILE_FORMAT}:
            intents = _collect_external_access_intents(classified, context)
            if intents is None:
                return "deny", f"Path traversal blocked for '{classified.name}'.", ()
            if intents:
                defer_reason = _reason_for(classified, "ask")
                return "defer", defer_reason, tuple(intents)
            return "allow", None, ()
        if classified.name == "bash":
            return (*shell_sandbox_precheck(classified.args, context, shell="bash"), ())
        if classified.name == "powershell":
            return (*shell_sandbox_precheck(classified.args, context, shell="powershell"), ())
    return "allow", None, ()


def _collect_external_access_intents(classified: ClassifiedToolCall, context: PermissionContext) -> list[AccessIntent] | None:
    """Resolve access for every file path of a path tool; return external intents or None on deny."""
    name = classified.name
    read_tools = {"read", "lsp"}
    require_exists = name in {"read", "manage", "lsp", "lsp_format"}
    allow_missing_write_file = name in {"write", "replace", "manage"}
    intents: list[AccessIntent] = []
    for file_path in file_paths_for_tool(name, classified.args):
        access = "read" if name in read_tools else "write"
        resolution = resolve_access(
            context.workspace,
            file_path,
            access=access,
            access_grants=context.access_grants,
            require_exists=require_exists,
            allow_missing_write_file=allow_missing_write_file,
        )
        if resolution.action == "deny":
            return None
        if resolution.action == "defer" and resolution.intent is not None:
            intents.append(resolution.intent)
    return intents


def session_action_for_tool(classified: ClassifiedToolCall, context: PermissionContext) -> Action | None:
    if any(session_rule_matches(classified, rule) for rule in context.session_deny):
        return "deny"
    if any(session_rule_matches(classified, rule) for rule in context.session_allow):
        return "allow"
    return None


def strategy_action_for_tool(classified: ClassifiedToolCall, context: PermissionContext) -> Action:
    if classified.capability in {PermissionCapability.BASH_READ, PermissionCapability.GIT_READ}:
        return "allow"
    permission = "edit" if classified.name in {"manage", "write", "replace"} else classified.name
    return evaluate(permission, classified.pattern, BASIC_RULES).action


def _decision(
    classified: ClassifiedToolCall,
    action: Action,
    source: str,
    reason: str = "",
    *,
    failure_check: bool = False,
    context: PermissionContext | None = None,
    access_intents: tuple[AccessIntent, ...] = (),
) -> PermissionDecision:
    risk = _risk_for(classified, action, reason, context=context, access_intents=access_intents)
    allowed_scopes = ()
    default_scope = None
    if risk.level == RiskLevel.BLOCKED and action != "deny":
        action = "blocked_ack"
        reason = risk.reason
    elif action == "ask":
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
        access_intents=access_intents,
    )



def _preset_decision_for(risk: RiskAssessment, context: PermissionContext | None):
    raw = context.permission_mode if context else PermissionMode.SAFE.value
    try:
        preset = PermissionMode(raw)
    except ValueError:
        preset = PermissionMode.SAFE
    return resolve_mode_decision(preset, risk)

def _risk_for(
    classified: ClassifiedToolCall,
    action: Action,
    reason: str,
    *,
    context: PermissionContext | None = None,
    access_intents: tuple[AccessIntent, ...] = (),
) -> RiskAssessment:
    if classified.name == "bash":
        return classify_shell_risk(
            str(classified.args.get("command") or ""),
            shell="bash",
            workspace=context.workspace if context else None,
        )
    if classified.name == "powershell":
        return classify_shell_risk(str(classified.args.get("command") or ""), shell="powershell")
    if action == "allow":
        return RiskAssessment.normal(tool_name=classified.name, pattern=classified.pattern, tags=(RiskTag.SAFE_READ,), reason=reason)
    if action == "deny":
        return RiskAssessment.blocked(tool_name=classified.name, pattern=classified.pattern, tags=(), reason=reason)
    tags: list[RiskTag] = []
    if classified.capability in {PermissionCapability.FILE_WRITE, PermissionCapability.FILE_FORMAT}:
        tags.append(RiskTag.WORKSPACE_EDIT)
    if any(not intent.is_workspace_path and not intent.grant_matched for intent in access_intents):
        tags.append(RiskTag.EXTERNAL_PATH)
    if classified.name == "git":
        subcommand = git_policy_for_args(classified.args).subcommand
        if subcommand == "push":
            tags.append(RiskTag.GIT_PUSH)
        elif subcommand in {"fetch", "pull"}:
            tags.append(RiskTag.NETWORK)
    return RiskAssessment.dangerous(
        tool_name=classified.name,
        pattern=classified.pattern,
        tags=tuple(tags),
        reason=reason,
    )


def _reason_for(classified: ClassifiedToolCall, action: Action) -> str:
    if action == "deny":
        return f"Permission denied: {classified.name} → {classified.pattern}"
    if action == "allow":
        return f"Permission allowed: {classified.name} → {classified.pattern}"
    return f"Permission required: {classified.name} → {classified.pattern}"

async def _maybe_await(value: Awaitable[Any] | Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _approval_precondition(access_grants: AccessGrants) -> ApprovalPrecondition:
    return ApprovalPrecondition(
        permission_mode=access_grants.permission_mode,
        revocation_epoch=access_grants.revocation_epoch,
    )


async def _release_lock(lock: Any | None) -> None:
    if lock is None:
        return
    release = getattr(lock, "release", None)
    if release is not None:
        await _maybe_await(release())


async def _call_add_grant(
    add_grant: Callable[..., Any],
    grant: AccessGrant,
    precondition: ApprovalPrecondition,
) -> Any:
    try:
        signature = inspect.signature(add_grant)
        accepts_precondition = (
            "precondition" in signature.parameters
            or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
        )
    except (TypeError, ValueError):
        accepts_precondition = True
    if accepts_precondition:
        return await _maybe_await(add_grant(grant, precondition=precondition))
    return await _maybe_await(add_grant(grant))


def _grant_choice(choice: str) -> tuple[GrantPersistence, ObjectType]:
    persistence: GrantPersistence = "persistent" if choice.startswith("persistent_") else "session"
    object_type: ObjectType = "dir" if choice.endswith("_dir") else "file"
    return persistence, object_type


async def authorized_path(
    ctx,
    file_path: str,
    *,
    write: bool,
    require_exists: bool = False,
    allow_missing_write_file: bool = False,
    prompt_label: str | None = None,
    allow_description: str | None = None,
    deny_description: str | None = None,
) -> tuple[Path | None, ToolResult | None]:
    authorization = ctx.authorization_service
    access = "write" if write else "read"
    access_grants = authorization.access_grants()
    precondition = _approval_precondition(access_grants)
    resolution = resolve_access(
        ctx.workspace,
        file_path,
        access=access,
        access_grants=access_grants,
        require_exists=require_exists,
        allow_missing_write_file=allow_missing_write_file,
    )
    if resolution.action == "allow" and resolution.intent is not None:
        return resolution.intent.normalized_path, None
    if resolution.action == "deny":
        return None, ToolResult(
            output=resolution.reason or f"Path traversal blocked: {file_path}",
            metadata={"error": True},
        )
    label = prompt_label or ("Write" if write else "Read")
    if authorization.interaction is None:
        return None, ToolResult(output=f"Path traversal blocked: {file_path}", metadata={"error": True})

    lock = None
    if authorization.target_locker is not None and resolution.intent is not None:
        lock = await _maybe_await(authorization.target_locker([resolution.intent.normalized_path]))
        access_grants = authorization.access_grants()
        precondition = _approval_precondition(access_grants)
        resolution = resolve_access(
            ctx.workspace,
            file_path,
            access=access,
            access_grants=access_grants,
            require_exists=require_exists,
            allow_missing_write_file=allow_missing_write_file,
        )
        if resolution.action == "allow" and resolution.intent is not None:
            await _release_lock(lock)
            return resolution.intent.normalized_path, None
        if resolution.action == "deny":
            await _release_lock(lock)
            return None, ToolResult(
                output=resolution.reason or f"Path traversal blocked: {file_path}",
                metadata={"error": True},
            )
    try:
        options = [
            ("Yes", "allow", allow_description or f"Allow this {access} once"),
            ("No", "deny", deny_description or f"Do not {access} this file"),
        ]
        if authorization.grant_writer is not None:
            options = [
                ("Session file", "session_file", allow_description or f"Allow this {access} file for this session"),
                ("Session dir", "session_dir", f"Allow this {access} directory for this session"),
                ("Persistent file", "persistent_file", f"Always allow this {access} file"),
                ("Persistent dir", "persistent_dir", f"Always allow this {access} directory"),
                ("Once", "allow", f"Allow this {access} once"),
                ("No", "deny", deny_description or f"Do not {access} this file"),
            ]
        response = await authorization.interaction.request(
            UserInteraction(
                type="choice",
                title=f"{label} outside workspace?",
                prompt=f"{label} file outside workspace? {resolution.intent.normalized_path if resolution.intent else file_path}",
                options=options,
            )
        )
        if response.cancelled or response.value in {None, "deny"}:
            return None, ToolResult(output=f"Path traversal blocked: {file_path}", metadata={"error": True})
        if response.value != "allow" and authorization.grant_writer is not None and resolution.intent is not None:
            persistence, object_type = _grant_choice(response.value)
            grant = grant_for_intent(
                resolution.intent,
                persistence=persistence,
                object_type=object_type,
            )
            if authorization.target_locker is not None:
                await _release_lock(lock)
                lock = await _maybe_await(
                    authorization.target_locker(
                        [resolution.intent.normalized_path],
                        final_paths=[grant.path],
                    )
                )
                access_grants = authorization.access_grants()
                precondition = _approval_precondition(access_grants)
                resolution = resolve_access(
                    ctx.workspace,
                    file_path,
                    access=access,
                    access_grants=access_grants,
                    require_exists=require_exists,
                    allow_missing_write_file=allow_missing_write_file,
                )
                if resolution.action == "allow" and resolution.intent is not None:
                    return resolution.intent.normalized_path, None
                if resolution.action == "deny":
                    return None, ToolResult(
                        output=resolution.reason or f"Path traversal blocked: {file_path}",
                        metadata={"error": True},
                    )
            result = await _call_add_grant(authorization.grant_writer, grant, precondition)
            if getattr(result, "ok", True) is False:
                return None, ToolResult(
                    output=getattr(result, "error", "Permission grant conflict") or "Permission grant conflict",
                    metadata={"error": True, "conflict": getattr(result, "conflict", False)},
                )
        return resolution.intent.normalized_path, None
    finally:
        await _release_lock(lock)


def sandbox_paths_for_access(ctx, *, write: bool) -> list[str]:
    return ctx.authorization_service.sandbox_paths(write=write)


__all__ = [
    "authorized_path",
    "authorize_tool_call",
    "decide_base_action",
    "sandbox_denial_reason",
    "sandbox_paths_for_access",
]
