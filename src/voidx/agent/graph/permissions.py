"""Tool permission UI adapter for the agent graph."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, TYPE_CHECKING

from voidx.config import PermissionMode
from voidx.permission.ai_approval import is_ai_approval_candidate
from voidx.permission.service import (
    PermissionContext,
    authorize_tool_call,
    classify_tool_call,
)
from voidx.permission.context import PermissionDecision
from voidx.permission.session_rules import scoped_session_rule_for_decision
from voidx.permission.schema import Action
from voidx.permission.risk import ApprovalScope, RiskLevel
from voidx.workflow.types import WorkflowRunState, WorkflowRunStatus
from voidx.runtime.intent import PersonaName
from voidx.runtime.ui import PermissionPromptCleared, PermissionPromptShown, PermissionToolDetail

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphPermissionHost



def _tool_call_key(tool_call: dict[str, Any]) -> str | None:
    try:
        payload = {
            "name": str(tool_call.get("name") or ""),
            "args": tool_call.get("args") or {},
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return None



class GraphPermissionMixin:
    _needs_failure_check: dict[str, dict]

    async def _authorize_tool_calls(
        self: GraphPermissionHost,
        tool_calls: list[dict],
        *,
        runtime_persona: str = PersonaName.COORDINATE,
        plan_mode: bool,
        session_id: str,
        interaction_mode: str | None = None,
        workflow_runs: object = (),
    ) -> tuple[list[dict], list[tuple[dict, str]]]:
        if getattr(self, "_successful_dangerous_calls_session_id", None) != session_id:
            self._successful_dangerous_calls.clear()
            self._successful_dangerous_calls_session_id = session_id
        approved: list[dict] = []
        denied: list[tuple[dict, str]] = []
        need_ask: list[PermissionDecision] = []
        context = PermissionContext.from_service(
            self._permission,
            workspace=self._workspace,
            interaction_mode=interaction_mode,
            plan_mode=plan_mode,
        )

        for tc in tool_calls:
            decision = authorize_tool_call(tc, context)
            if (
                decision.action == Action.ASK
                and decision.risk is not None
                and decision.risk.level == RiskLevel.DANGEROUS
                and getattr(self._permission, "permission_mode", "") == PermissionMode.AI_APPROVAL.value
                and _tool_call_key(tc) in getattr(self, "_successful_dangerous_calls", set())
            ):
                approved.append(_tool_call_with_approval_risk(decision, approved_by="cached"))
            elif decision.action == Action.ALLOW:
                approved_call = _tool_call_with_execution_approval(decision)
                approved.append(approved_call)
                if decision.failure_check:
                    self._needs_failure_check[approved_call.get("id", "")] = approved_call
            elif decision.action == Action.DEFER:
                approved.append(decision.tool_call)
            elif decision.action == Action.DENY:
                denied.append((decision.tool_call, decision.reason))
            elif decision.action == Action.BLOCKED_ACK:
                need_ask.append(decision)
            else:
                need_ask.append(decision)

        if need_ask:
            await self._ask_and_apply_permission(need_ask, approved, denied)

        return approved, denied

    async def _ask_and_apply_permission(
        self: GraphPermissionHost,
        need_ask: list[PermissionDecision],
        approved: list[dict],
        denied: list[tuple[dict, str]],
    ) -> None:
        blocked = [decision for decision in need_ask if decision.action == Action.BLOCKED_ACK]
        ai_allowed: list[PermissionDecision] = []
        if (
            getattr(self._permission, "permission_mode", "") == PermissionMode.AI_APPROVAL.value
            and getattr(self, "_settings", None) is not None
            and getattr(self, "_ai_approval", None) is not None
        ):
            candidates = [
                decision for decision in need_ask
                if is_ai_approval_candidate(decision)
            ]
            if candidates:
                result = await self._ai_approval.review(candidates, self._settings)
                allowed_ids = result.allowed_ids if result.reason == "reviewed" else frozenset()
                for decision in candidates:
                    if decision.tool_call.get("id") in allowed_ids:
                        ai_allowed.append(decision)
                        approved.append(_tool_call_with_approval_risk(decision, approved_by="ai"))
                        self._notice_permission_result(f"AI 审批: allow {decision.name}")
                        if hasattr(self._permission, "inc_ai_approval_count"):
                            self._permission.inc_ai_approval_count()
                need_ask = _attach_ai_approval_failures(need_ask, candidates, result, allowed_ids)
            if ai_allowed:
                if self._ui.via_events():
                    from voidx.runtime.ui import RefreshRequested
                    await self._ui.events.emit(RefreshRequested())
                need_ask = [decision for decision in need_ask if decision not in ai_allowed]

        approvable = [decision for decision in need_ask if decision.action != Action.BLOCKED_ACK]

        if blocked:
            await self._ask_tool_permission(blocked)
            if self._ui.via_events():
                await self._ui.events.emit(PermissionPromptCleared())
            for decision in blocked:
                denied.append((decision.tool_call, decision.reason or "Blocked command"))

        if not approvable:
            return

        choice = await self._ask_tool_permission(approvable)
        if choice is None:
            choice = "n"

        if self._ui.via_events():
            await self._ui.events.emit(PermissionPromptCleared())

        tool_calls = [_tool_call_with_approval_risk(decision) for decision in approvable]
        if choice == "a" and _all_decisions_allow_scope(approvable, ApprovalScope.SESSION):
            for decision in approvable:
                self._permission.allow_silent(scoped_session_rule_for_decision(decision))
            approved.extend(tool_calls)
        elif choice == "y":
            approved.extend(tool_calls)
        else:
            self._notice_permission_result(f"{len(need_ask)} tools denied")
            for tc in tool_calls:
                denied.append((tc, f"User denied: {tc['name']}"))

    async def _ask_tool_permission(self: GraphPermissionHost, tool_calls: list[dict] | list[PermissionDecision]) -> str | None:
        decisions = [_coerce_permission_decision(item) for item in tool_calls]
        raw_tool_calls = [decision.tool_call for decision in decisions]
        tool_list = ", ".join(t["name"] for t in raw_tool_calls)
        choices = _permission_choices(decisions)
        details = [item.model_dump(mode="json") for item in self._permission_tool_details(decisions)]

        if self._ui.via_events():
            await self._ui.events.emit(PermissionPromptShown(
                prompt=f"Allow tools: {tool_list}?",
                choices=choices,
                tools=self._permission_tool_details(decisions),
            ))

        if not self._app:
            self._ui.ui.print("")
            self._ui.ui.print(f"  [yellow]Allow tools: [bold]{tool_list}[/bold]?[/yellow]")

        if self._app:
            return await self._app.ask_choice("Allow tool use?", choices, details=details)
        return "n"


    def _show_permission_output(self: GraphPermissionHost, message: str) -> bool:
        dock = getattr(getattr(self, "_ui", None), "dock", None)
        append = getattr(dock, "append_message", None)
        if not callable(append):
            return False
        append(message)
        return True
    def _notice_permission_result(self: GraphPermissionHost, message: str) -> None:
        if self._show_permission_output(message):
            return
        self._ui.ui.print(f"[dim]✓ {message}[/dim]")

    def _notify_tool_failure(self: GraphPermissionHost, tc: dict, result) -> None:
        """Notify user when an auto-approved tool fails."""
        tool_name = tc.get("name", "unknown")
        error_preview = str(result.output)[:200]
        message = f"[on-failure] '{tool_name}' failed: {error_preview}"
        if not self._show_permission_output(message):
            self._ui.ui.print(f"\n[yellow]{message}[/yellow]")

    def _record_successful_tool_call(self: GraphPermissionHost, tool_call: dict[str, Any]) -> None:
        risk = (tool_call.get("metadata") or {}).get("approved_risk") or {}
        if risk.get("risk_level") != RiskLevel.DANGEROUS.value:
            return
        key = _tool_call_key(tool_call)
        if key is not None:
            self._successful_dangerous_calls.add(key)

    def clear_successful_dangerous_calls(self: GraphPermissionHost) -> None:
        self._successful_dangerous_calls.clear()
        self._successful_dangerous_calls_session_id = None


    def _clear_failure_check(self: GraphPermissionHost, cid: str) -> None:
        """Remove a tool call ID from on-failure tracking (used on success)."""
        self._needs_failure_check.pop(cid, None)

    def _permission_tool_details(self: GraphPermissionHost, decisions: list[PermissionDecision]) -> list[PermissionToolDetail]:
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


def _coerce_permission_decision(item: dict | PermissionDecision) -> PermissionDecision:
    if isinstance(item, PermissionDecision):
        return item
    classified = classify_tool_call(item)
    return PermissionDecision(
        action=Action.ASK,
        tool_call=classified.tool_call,
        name=classified.name,
        args=classified.args,
        pattern=classified.pattern,
        capability=classified.capability,
        source="compat",
    )


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


def _permission_choices(decisions: list[PermissionDecision]) -> list[tuple[str, str, str]]:
    if _all_decisions_blocked_ack(decisions):
        return [("Do not run", "n", "This command is blocked")]
    choices: list[tuple[str, str, str]] = []
    if _all_decisions_allow_scope(decisions, ApprovalScope.SESSION):
        choices.append(("Yes, always", "a", "Allow these tools for this session"))
    choices.append(("Yes", "y", "Allow this tool use once"))
    choices.append(("No", "n", "Deny these tools"))
    return choices


def _all_decisions_allow_scope(decisions: list[PermissionDecision], scope: str) -> bool:
    if not decisions:
        return False
    return all(scope in _scope_values(decision.allowed_scopes) for decision in decisions)


def _all_decisions_blocked_ack(decisions: list[PermissionDecision]) -> bool:
    return bool(decisions) and all(decision.action == Action.BLOCKED_ACK for decision in decisions)


def _scope_values(scopes: tuple[object, ...]) -> set[str]:
    return {scope.value if hasattr(scope, "value") else str(scope) for scope in scopes}
