"""Tool permission UI adapter for the agent graph."""

from __future__ import annotations

from fnmatch import fnmatch
from typing import TYPE_CHECKING

from voidx.agent.graph.workflow_utils import active_workflow_names
from voidx.permission.service import (
    PermissionContext,
    authorize_tool_call,
    classify_tool_call,
)
from voidx.permission.context import PermissionDecision
from voidx.permission.rules import PermissionCapability
from voidx.workflow.service import workflow_gate, workflow_sort_key
from voidx.workflow.types import WorkflowRunState, WorkflowRunStatus
from voidx.runtime.ui import PermissionPromptCleared, PermissionPromptShown, PermissionToolDetail

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphPermissionHost


class GraphPermissionMixin:
    _needs_failure_check: dict[str, dict]

    async def _authorize_tool_calls(
        self: GraphPermissionHost,
        tool_calls: list[dict],
        *,
        runtime_persona: str = "coordinate",
        plan_mode: bool,
        session_id: str,
        interaction_mode: str | None = None,
        workflow_runs: object = (),
    ) -> tuple[list[dict], list[tuple[dict, str]]]:
        approved: list[dict] = []
        denied: list[tuple[dict, str]] = []
        need_ask: list[PermissionDecision] = []
        active_workflows = active_workflow_names(workflow_runs)

        context = PermissionContext.from_service(
            self._permission,
            workspace=self._workspace,
            interaction_mode=interaction_mode,
            plan_mode=plan_mode,
        )

        for tc in tool_calls:
            classified = classify_tool_call(tc)
            gate_requires_approval = _workflow_gate_requires_approval(classified, active_workflows)
            gate_allows_without_approval = _workflow_gate_allows_without_approval(classified, active_workflows)
            decision = authorize_tool_call(tc, context)
            if gate_allows_without_approval and decision.action in {"allow", "ask"}:
                approved.append(decision.tool_call)
                if decision.failure_check:
                    self._needs_failure_check[decision.tool_call.get("id", "")] = decision.tool_call
                continue
            if decision.action == "allow":
                if (
                    gate_requires_approval
                    or (
                        not gate_allows_without_approval
                        and decision.source != "session"
                        and _persona_requires_approval(classified.capability, runtime_persona or "coordinate")
                    )
                ):
                    need_ask.append(decision)
                    continue
                approved.append(decision.tool_call)
                if decision.failure_check:
                    self._needs_failure_check[decision.tool_call.get("id", "")] = decision.tool_call
            elif decision.action == "defer":
                approved.append(decision.tool_call)
            elif decision.action == "deny":
                denied.append((decision.tool_call, decision.reason))
            elif decision.action == "blocked_ack":
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
        blocked = [decision for decision in need_ask if decision.action == "blocked_ack"]
        approvable = [decision for decision in need_ask if decision.action != "blocked_ack"]

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
        if choice == "a" and _all_decisions_allow_scope(approvable, "session"):
            for tc in tool_calls:
                self._permission.allow_silent(tc["name"])
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

    def _notice_permission_result(self: GraphPermissionHost, message: str) -> None:
        if self._show_permission_output(message):
            return
        self._ui.ui.print(f"[dim]✓ {message}[/dim]")

    def _notify_tool_failure(self: GraphPermissionHost, tc: dict, result) -> None:
        """Notify user when an auto-approved tool (on-failure policy) fails.

        The tool was auto-allowed by the on-failure policy and then
        actually failed.  Let the user know so they can decide whether
        to abort or let the agent retry.
        """
        tool_name = tc.get("name", "unknown")
        error_preview = str(result.output)[:200]
        message = f"[on-failure] '{tool_name}' failed: {error_preview}"
        if not self._show_permission_output(message):
            self._ui.ui.print(f"\n[yellow]{message}[/yellow]")

    def _show_permission_output(self: GraphPermissionHost, message: str) -> bool:
        self._ui.dock.append_message(message)
        return True

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
            ))
        return details



def _coerce_permission_decision(item: dict | PermissionDecision) -> PermissionDecision:
    if isinstance(item, PermissionDecision):
        return item
    classified = classify_tool_call(item)
    return PermissionDecision(
        action="ask",
        tool_call=classified.tool_call,
        name=classified.name,
        args=classified.args,
        pattern=classified.pattern,
        capability=classified.capability,
        source="compat",
    )


def _tool_call_with_approval_risk(decision: PermissionDecision) -> dict:
    if decision.risk is None or decision.risk.level.value == "blocked":
        return decision.tool_call
    metadata = dict(decision.tool_call.get("metadata") or {})
    metadata["approved_risk"] = {
        "tool_name": decision.name,
        "pattern": decision.pattern,
        "risk_level": decision.risk.level.value,
        "tags": [tag.value for tag in decision.risk.tags],
        "reason": decision.risk.reason,
    }
    return {**decision.tool_call, "metadata": metadata}


def _permission_choices(decisions: list[PermissionDecision]) -> list[tuple[str, str, str]]:
    if _all_decisions_blocked_ack(decisions):
        return [("Do not run", "n", "This command is blocked")]
    choices: list[tuple[str, str, str]] = []
    if _all_decisions_allow_scope(decisions, "session"):
        choices.append(("Yes, always", "a", "Allow these tools for this session"))
    choices.append(("Yes", "y", "Allow this tool use once"))
    choices.append(("No", "n", "Deny these tools"))
    return choices


def _all_decisions_allow_scope(decisions: list[PermissionDecision], scope: str) -> bool:
    if not decisions:
        return False
    return all(scope in _scope_values(decision.allowed_scopes) for decision in decisions)


def _all_decisions_blocked_ack(decisions: list[PermissionDecision]) -> bool:
    return bool(decisions) and all(decision.action == "blocked_ack" for decision in decisions)


def _scope_values(scopes: tuple[object, ...]) -> set[str]:
    return {scope.value if hasattr(scope, "value") else str(scope) for scope in scopes}

def _workflow_gate_requires_approval(classified, active_workflows: list[str]) -> bool:
    workflow = _current_workflow_name(active_workflows)
    if not workflow:
        return False
    gate = workflow_gate(workflow)
    if gate is None or classified.name not in gate.denied_tools:
        return False
    if _matches_allowed_path(classified.args.get("file_path", ""), gate.allowed_paths):
        return False
    return True


def _workflow_gate_allows_without_approval(classified, active_workflows: list[str]) -> bool:
    workflow = _current_workflow_name(active_workflows)
    if not workflow:
        return False
    gate = workflow_gate(workflow)
    if gate is None or classified.name not in gate.denied_tools:
        return False
    return _matches_allowed_path(classified.args.get("file_path", ""), gate.allowed_paths)


def _current_workflow_name(active_workflows: list[str]) -> str:
    if not active_workflows:
        return ""
    return sorted(active_workflows, key=workflow_sort_key)[-1]


def _matches_allowed_path(file_path: object, patterns: tuple[str, ...]) -> bool:
    if not isinstance(file_path, str) or not file_path.strip():
        return False
    normalized = file_path.strip().replace("\\", "/")
    return any(fnmatch(normalized, pattern) for pattern in patterns)


def _persona_requires_approval(capability: PermissionCapability, runtime_persona: str) -> bool:
    if capability not in {PermissionCapability.FILE_WRITE, PermissionCapability.FILE_FORMAT}:
        return False
    personas = {
        item.strip()
        for item in (runtime_persona or "coordinate").split(",")
        if item.strip()
    }
    return "implement" not in personas
