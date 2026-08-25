"""Tool permission authorization flow for LangGraph execution."""

from __future__ import annotations

import inspect
import json
import uuid
from dataclasses import replace
from typing import Any

from voidx.agent.domain.ui_events import PermissionPromptCleared, PermissionPromptShown, PermissionToolDetail, RefreshRequested
from voidx.tooling.domain.permission import PermissionMode
from voidx.observability.tool_log import log_tool_event
from voidx.tooling.application.ai_approval import is_ai_approval_candidate
from voidx.tooling.application.permission_service import (
    PermissionContext,
    authorize_tool_call,
    classify_tool_call,
)
from voidx.tooling.domain.authorization import PermissionDecision
from voidx.tooling.domain.grants import AccessIntent, ApprovalPrecondition
from voidx.tooling.policy.filesystem.grants import grant_for_intent
from voidx.tooling.policy.permission.session_rules import scoped_session_rule_for_decision
from voidx.tooling.domain.permission import Action
from voidx.tooling.domain.risk import ApprovalScope, RiskLevel
from voidx.agent.domain.task.intent import PersonaName
from voidx.agent.domain.tool_policy import ProfileToolPolicy
from voidx.agent.adapters.tools.permission_projection import project_agent_tool_call
from voidx.agent.adapters.langgraph.runtime.tool_policy_bridge import check_tool_policy
from voidx.agent.adapters.langgraph.runtime.thread_context import (
    current_thread_execution_state,
    tool_registry_for,
)


def _attach_ai_approval_failures(
    decisions: list[PermissionDecision],
    candidates: list[PermissionDecision],
    result: Any,
    allowed_ids: frozenset[str],
) -> list[PermissionDecision]:
    candidate_ids = {str(decision.tool_call.get("id") or "") for decision in candidates}
    failures = {
        call_id: _ai_approval_failure_message(result, call_id)
        for call_id in candidate_ids
        if call_id and call_id not in allowed_ids
    }
    if not failures:
        return decisions
    return [
        replace(decision, ai_approval_failure=failures[call_id])
        if (call_id := str(decision.tool_call.get("id") or "")) in failures
        else decision
        for decision in decisions
    ]


def _coerce_permission_decision(item: dict | PermissionDecision) -> PermissionDecision:
    if isinstance(item, PermissionDecision):
        return item
    classified = classify_tool_call(project_agent_tool_call(item))
    return PermissionDecision(
        action=Action.ASK,
        tool_call=classified.tool_call,
        name=classified.name,
        args=classified.args,
        pattern=classified.pattern,
        capability=classified.capability,
        source="compat",
    )


def _permission_choices(decisions: list[PermissionDecision]) -> list[tuple[str, str, str]]:
    if _all_decisions_blocked_ack(decisions):
        return [("Do not run", "n", "This command is blocked")]
    external_intents = _external_access_intents(decisions)
    if len(external_intents) == 1:
        return _path_grant_choices(external_intents[0])
    if external_intents:
        return [("Allow once", "allow", "Allow this tool use once"), ("Deny", "deny", "Deny these tools")]
    choices: list[tuple[str, str, str]] = []
    if _all_decisions_allow_scope(decisions, ApprovalScope.SESSION):
        choices.append(("Yes, always", "a", "Allow these tools for this session"))
    choices.append(("Yes", "y", "Allow this tool use once"))
    choices.append(("No", "n", "Deny these tools"))
    return choices


def _external_access_intents(decisions: list[PermissionDecision]) -> list[AccessIntent]:
    intents: list[AccessIntent] = []
    for decision in decisions:
        for intent in decision.access_intents:
            if not intent.is_workspace_path and not intent.grant_matched:
                intents.append(intent)
    return intents


def _path_grant_choices(intent: AccessIntent) -> list[tuple[str, str, str]]:
    access = intent.access
    return [
        ("Allow once", "allow", f"Allow this {access} once"),
        ("This file this session", "session_file", f"Allow this {access} file for this session"),
        ("This folder this session", "session_dir", f"Allow this {access} directory for this session"),
        ("Always allow this file", "persistent_file", f"Always allow this {access} file"),
        ("Always allow this folder", "persistent_dir", f"Always allow this {access} directory"),
        ("Deny", "deny", f"Do not {access} this file"),
    ]


_PATH_GRANT_CHOICES = frozenset({"session_file", "session_dir", "persistent_file", "persistent_dir"})


_GRANT_PERSISTENCE_MAP = {
    "session_file": "session",
    "session_dir": "session",
    "persistent_file": "persistent",
    "persistent_dir": "persistent",
    "runtime_file": "runtime",
    "runtime_dir": "runtime",
}

_GRANT_OBJECT_TYPE_MAP = {
    "session_file": "file",
    "session_dir": "dir",
    "persistent_file": "file",
    "persistent_dir": "dir",
    "runtime_file": "file",
    "runtime_dir": "dir",
}


async def _apply_path_grant_choice(
    host: Any,
    decisions: list[PermissionDecision],
    choice: str,
    *,
    precondition: ApprovalPrecondition | None = None,
) -> bool:
    persistence = _GRANT_PERSISTENCE_MAP.get(choice, "runtime")
    object_type = _GRANT_OBJECT_TYPE_MAP.get(choice, "file")
    for decision in decisions:
        for intent in decision.access_intents:
            if intent.is_workspace_path or intent.grant_matched:
                continue
            grant = grant_for_intent(intent, persistence, object_type=object_type)
            result = await host._permission.add_grant(grant, precondition=precondition)
            if getattr(result, "ok", True) is False:
                return False
    return True


async def _apply_runtime_grant(
    host: Any,
    decisions: list[PermissionDecision],
    *,
    precondition: ApprovalPrecondition | None = None,
) -> bool:
    for decision in decisions:
        for intent in decision.access_intents:
            if intent.is_workspace_path or intent.grant_matched:
                continue
            choice = "runtime_dir" if intent.object_type == "dir" else "runtime_file"
            applied = await _apply_path_grant_choice(
                host,
                [decision],
                choice,
                precondition=precondition,
            )
            if not applied:
                return False
    return True


def _ai_approval_failure_message(result: Any, call_id: str) -> str:
    skipped_reason = getattr(result, "skipped_reasons", {}).get(call_id, "")
    if skipped_reason:
        return f"AI approval skipped: {skipped_reason}; requesting human review."
    if getattr(result, "reason", "") == "reviewed":
        reason = getattr(result, "denied_reasons", {}).get(call_id, "")
        if reason:
            return f"AI approval denied: {reason}"
        if call_id in getattr(result, "reviewed_ids", frozenset()):
            return "AI approval denied; requesting human review."
        return "AI approval skipped: candidate was not reviewed; requesting human review."
    failure = {
        "disabled": "disabled",
        "unavailable": "unavailable",
        "invalid_response": "returned an invalid response",
        "skipped": "skipped before review",
        "timeout": "timed out",
        "connection_error": "connection error",
        "error": "internal error",
    }.get(getattr(result, "reason", ""), "unknown error")
    return f"AI approval failed: {failure}; requesting human review."


def _tool_call_key(tool_call: dict[str, Any]) -> str | None:
    try:
        payload = {
            "name": str(tool_call.get("name") or ""),
            "args": tool_call.get("args") or {},
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _all_decisions_allow_scope(decisions: list[PermissionDecision], scope: str) -> bool:
    if not decisions:
        return False
    return all(scope in _scope_values(decision.allowed_scopes) for decision in decisions)


def _tool_call_with_approval_risk(decision: PermissionDecision, *, approved_by: str = "user") -> dict:
    if decision.risk is None or decision.risk.level == RiskLevel.BLOCKED:
        return decision.tool_call
    metadata = dict(decision.tool_call.get("metadata") or {})
    metadata["approved_risk"] = {
        "tool_name": decision.name,
        "pattern": decision.pattern,
        "risk_level": decision.risk.level.value,
        "tags": [tag.value for tag in decision.risk.tags],
        "reason": decision.risk.reason,
        **({"approved_by": approved_by} if approved_by != "user" else {}),
    }
    return {**decision.tool_call, "metadata": metadata}


def _tool_call_with_execution_approval(decision: PermissionDecision) -> dict:
    if decision.name not in {"bash", "powershell"}:
        return decision.tool_call
    if decision.risk is None or decision.risk.level == RiskLevel.NORMAL:
        return decision.tool_call
    return _tool_call_with_approval_risk(decision)


def _all_decisions_blocked_ack(decisions: list[PermissionDecision]) -> bool:
    return bool(decisions) and all(decision.action == Action.BLOCKED_ACK for decision in decisions)


def _scope_values(scopes: tuple[object, ...]) -> set[str]:
    return {scope.value if hasattr(scope, "value") else str(scope) for scope in scopes}


def _permission_request_id() -> str:
    return f"permission_{uuid.uuid4().hex}"


async def _call_with_optional_kwargs(callable_obj, *args, **kwargs):
    try:
        parameters = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return await callable_obj(*args, **kwargs)
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return await callable_obj(*args, **kwargs)
    filtered = {key: value for key, value in kwargs.items() if key in parameters}
    return await callable_obj(*args, **filtered)


class PermissionFlow:
    def __init__(self, host: Any) -> None:
        self.host = host

    async def _authorize_tool_calls(
        self: Any,
        tool_calls: list[dict],
        *,
        runtime_persona: str = PersonaName.COORDINATE,
        plan_mode: bool,
        session_id: str,
        interaction_mode: str | None = None,
        workflow_runs: object = (),
    ) -> tuple[list[dict], list[tuple[dict, str]]]:
        host = self.host
        if host._successful_dangerous_calls_session_id != session_id:
            host._successful_dangerous_calls.clear()
            host._successful_dangerous_calls_session_id = session_id
        state_context = current_thread_execution_state()
        chat_tool_view = getattr(state_context, "tool_policy", None) if state_context else None
        if chat_tool_view is not None:
            approved: list[dict] = []
            denied: list[tuple[dict, str]] = []
            defer_to_engine: list[dict] = []
            for tool_call in tool_calls:
                args = tool_call.get("args", {}) or {}
                decision = check_tool_policy(
                    chat_tool_view,
                    tool_registry_for(host),
                    str(tool_call.get("name", "")),
                    args,
                )
                if not decision.allowed:
                    denied.append((tool_call, f"Tool denied: {decision.reason}"))
                elif isinstance(chat_tool_view, ProfileToolPolicy):
                    defer_to_engine.append(tool_call)
                elif decision.requests_approval:
                    defer_to_engine.append(tool_call)
                else:
                    approved.append(tool_call)
            if not defer_to_engine:
                return approved, denied
            tool_calls = defer_to_engine
        else:
            approved: list[dict] = []
            denied: list[tuple[dict, str]] = []
        need_ask: list[PermissionDecision] = []
        context = host._permission.context_for(
            workspace=host._workspace,
            interaction_mode=interaction_mode,
            plan_mode=plan_mode,
        )

        for tc in tool_calls:
            args = tc.get("args", {}) if isinstance(tc.get("args", {}), dict) else {}
            autonomous_mcp_call = (
                isinstance(chat_tool_view, ProfileToolPolicy)
                and chat_tool_view.resource_policy.hitl_mode == "autonomous"
                and str(tc.get("name", "")) == "mcp"
                and str(args.get("op") or "").strip().lower() == "call"
            )
            call_context = (
                replace(context, execution_gated=True)
                if autonomous_mcp_call
                else context
            )
            decision = authorize_tool_call(
                project_agent_tool_call(tc),
                call_context,
            )
            autonomous = (
                isinstance(chat_tool_view, ProfileToolPolicy)
                and chat_tool_view.resource_policy.hitl_mode == "autonomous"
            )
            if autonomous and decision.action in {Action.ASK, Action.BLOCKED_ACK}:
                action = (
                    decision.action.value
                    if isinstance(decision.action, Action)
                    else str(decision.action)
                )
                denied.append((
                    decision.tool_call,
                    f"Autonomous authorization denied: {action}",
                ))
            elif (
                decision.action == Action.ASK
                and decision.risk is not None
                and decision.risk.level == RiskLevel.DANGEROUS
                and getattr(host._permission, "permission_mode", "") == PermissionMode.AI_APPROVAL.value
                and _tool_call_key(tc) in getattr(host, "_successful_dangerous_calls", set())
            ):
                approved.append(_tool_call_with_approval_risk(decision, approved_by="cached"))
            elif decision.action == Action.ALLOW:
                approved_call = _tool_call_with_execution_approval(decision)
                approved.append(approved_call)
                if decision.failure_check:
                    host._needs_failure_check[approved_call.get("id", "")] = approved_call
            elif decision.action == Action.DEFER:
                approved.append(decision.tool_call)
            elif decision.action == Action.DENY:
                denied.append((decision.tool_call, decision.reason))
            elif decision.action == Action.BLOCKED_ACK:
                need_ask.append(decision)
            else:
                need_ask.append(decision)

        if need_ask:
            await host._ask_and_apply_permission(need_ask, approved, denied)

        return approved, denied


    async def _ask_and_apply_permission(
        self: Any,
        need_ask: list[PermissionDecision],
        approved: list[dict],
        denied: list[tuple[dict, str]],
    ) -> None:
        host = self.host
        blocked = [decision for decision in need_ask if decision.action == Action.BLOCKED_ACK]
        ai_allowed: list[PermissionDecision] = []
        if (
            getattr(host._permission, "permission_mode", "") == PermissionMode.AI_APPROVAL.value
            and getattr(host, "_settings", None) is not None
            and getattr(host, "_ai_approval", None) is not None
        ):
            candidates = [
                decision for decision in need_ask
                if is_ai_approval_candidate(decision)
            ]
            if candidates:
                result = await host._ai_approval.review(candidates, host._settings)
                allowed_ids = result.allowed_ids if result.reason == "reviewed" else frozenset()
                for decision in candidates:
                    if decision.tool_call.get("id") in allowed_ids:
                        ai_allowed.append(decision)
                        approved.append(_tool_call_with_approval_risk(decision, approved_by="ai"))
                        await _apply_runtime_grant(host, [decision])
                        host._notice_permission_result(f"AI 审批: allow {decision.name}")
                        if hasattr(host._permission, "inc_ai_approval_count"):
                            host._permission.inc_ai_approval_count()
                need_ask = _attach_ai_approval_failures(need_ask, candidates, result, allowed_ids)
            if ai_allowed:
                if host._ui.via_events():
                    await host._ui.events.emit(RefreshRequested())
                need_ask = [decision for decision in need_ask if decision not in ai_allowed]

        approvable = [decision for decision in need_ask if decision.action != Action.BLOCKED_ACK]

        if blocked:
            request_id = _permission_request_id()
            try:
                await _call_with_optional_kwargs(
                    host._ask_tool_permission,
                    blocked,
                    request_id=request_id,
                )
            finally:
                if host._ui.via_events():
                    await host._ui.events.emit(PermissionPromptCleared(request_id=request_id))
            for decision in blocked:
                denied.append((decision.tool_call, decision.reason or "Blocked command"))

        if not approvable:
            return

        precondition = ApprovalPrecondition(
            permission_mode=host._permission.permission_mode,
            revocation_epoch=host._permission.revocation_epoch,
        )
        request_id = _permission_request_id()
        try:
            choice = await _call_with_optional_kwargs(
                host._ask_tool_permission,
                approvable,
                request_id=request_id,
            )
        finally:
            if host._ui.via_events():
                await host._ui.events.emit(PermissionPromptCleared(request_id=request_id))
        if choice is None:
            choice = "n"

        tool_calls = [_tool_call_with_approval_risk(decision) for decision in approvable]
        if choice == "a" and _all_decisions_allow_scope(approvable, ApprovalScope.SESSION):
            for decision in approvable:
                host._permission.allow_silent(scoped_session_rule_for_decision(decision))
            approved.extend(tool_calls)
        elif choice == "y":
            approved.extend(tool_calls)
        elif choice in _PATH_GRANT_CHOICES:
            applied = await _apply_path_grant_choice(
                host,
                approvable,
                choice,
                precondition=precondition,
            )
            if applied:
                approved.extend(tool_calls)
            else:
                for tc in tool_calls:
                    denied.append((tc, "Permission grant conflict"))
        elif choice == "allow":
            applied = await _apply_runtime_grant(host, approvable, precondition=precondition)
            if applied:
                approved.extend(tool_calls)
            else:
                for tc in tool_calls:
                    denied.append((tc, "Permission grant conflict"))
        else:
            host._notice_permission_result(f"{len(need_ask)} tools denied")
            for tc in tool_calls:
                denied.append((tc, f"User denied: {tc['name']}"))


    async def _ask_tool_permission(
        self: Any,
        tool_calls: list[dict] | list[PermissionDecision],
        request_id: str | None = None,
    ) -> str | None:
        host = self.host
        decisions = [_coerce_permission_decision(item) for item in tool_calls]
        raw_tool_calls = [decision.tool_call for decision in decisions]
        tool_list = ", ".join(t["name"] for t in raw_tool_calls)
        choices = _permission_choices(decisions)
        details = [item.model_dump(mode="json") for item in host._permission_tool_details(decisions)]
        request_id = request_id or _permission_request_id()

        if host._ui.via_events():
            await host._ui.events.emit(PermissionPromptShown(
                request_id=request_id,
                prompt=f"Allow tools: {tool_list}?",
                choices=choices,
                tools=host._permission_tool_details(decisions),
            ))

        choice = await _call_with_optional_kwargs(
            host._ui.ask_choice,
            "Allow tool use?",
            choices,
            details=details,
            request_id=request_id,
        )
        if choice is not None:
            return choice
        host._ui.ui.print("")
        host._ui.ui.print(f"  [yellow]Allow tools: [bold]{tool_list}[/bold]?[/yellow]")
        return "n"


    def _show_permission_output(self: Any, message: str) -> bool:
        host = self.host
        dock = getattr(getattr(host, "_ui", None), "dock", None)
        append = getattr(dock, "append_message", None)
        if not callable(append):
            return False
        append(message)
        return True


    def _notice_permission_result(self: Any, message: str) -> None:
        host = self.host
        log_tool_event("permission_notice", message=message)


    def _permission_tool_details(self: Any, decisions: list[PermissionDecision]) -> list[PermissionToolDetail]:
        host = self.host
        details: list[PermissionToolDetail] = []
        for decision in decisions:
            details.append(PermissionToolDetail(
                name=decision.name,
                pattern=decision.pattern,
                args=decision.args,
                risk=decision.risk.model_dump(mode="json") if decision.risk is not None else None,
                allowed_scopes=tuple(scope.value if hasattr(scope, "value") else str(scope) for scope in decision.allowed_scopes),
                default_scope=decision.default_scope.value if hasattr(decision.default_scope, "value") else decision.default_scope,
                ai_approval_failure=decision.ai_approval_failure,
            ))
        return details

