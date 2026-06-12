"""Tool permission UI adapter for the agent graph."""

from __future__ import annotations

from typing import TYPE_CHECKING

from voidx.permission.engine import (
    PermissionContext,
    authorize_tool_call,
    build_pattern,
    classify_tool_call,
)
from voidx.permission.rules import PermissionCapability
from voidx.workflow.policy import workflow_denied_tools, workflow_gate
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.ui.output.events.schema import PermissionToolDetail

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphPermissionHost


class GraphPermissionMixin:
    _needs_failure_check: dict[str, dict]

    async def _authorize_tool_calls(
        self: GraphPermissionHost,
        tool_calls: list[dict],
        agent_name: str,
        plan_mode: bool,
        session_id: str,
        interaction_mode: str | None = None,
        workflow_runs: object = (),
        runtime_persona: str | None = None,
    ) -> tuple[list[dict], list[tuple[dict, str]]]:
        approved: list[dict] = []
        denied: list[tuple[dict, str]] = []
        need_ask: list[dict] = []
        active_workflows = _active_workflow_names(workflow_runs)
        gate_denied = workflow_denied_tools(active_workflows)

        context = PermissionContext.from_service(
            self._permission,
            workspace=self._workspace,
            interaction_mode=interaction_mode,
            plan_mode=plan_mode,
        )

        for tc in tool_calls:
            if tc.get("name") in gate_denied:
                denied.append((tc, _gate_denial_reason(tc.get("name", ""), active_workflows)))
                continue
            classified = classify_tool_call(tc)
            decision = authorize_tool_call(tc, context)
            if decision.action == "allow":
                if (
                    decision.source != "session"
                    and _persona_requires_approval(classified.capability, runtime_persona or agent_name)
                ):
                    need_ask.append(decision.tool_call)
                    continue
                approved.append(decision.tool_call)
                if decision.failure_check:
                    self._needs_failure_check[decision.tool_call.get("id", "")] = decision.tool_call
            elif decision.action == "deny":
                denied.append((decision.tool_call, decision.reason))
            else:
                need_ask.append(decision.tool_call)

        if need_ask:
            await self._ask_and_apply_permission(need_ask, approved, denied)

        return approved, denied

    async def _ask_and_apply_permission(
        self: GraphPermissionHost,
        need_ask: list[dict],
        approved: list[dict],
        denied: list[tuple[dict, str]],
    ) -> None:
        choice = await self._ask_tool_permission(need_ask)
        if choice is None:
            choice = "n"

        if choice == "a":
            for tc in need_ask:
                self._permission.allow_silent(tc["name"])
            self._notice_permission_result(f"{len(need_ask)} tools allowed for this session")
            approved.extend(need_ask)
        elif choice == "y":
            self._notice_permission_result(f"{len(need_ask)} tools allowed once")
            approved.extend(need_ask)
        else:
            self._notice_permission_result(f"{len(need_ask)} tools denied")
            for tc in need_ask:
                denied.append((tc, f"User denied: {tc['name']}"))

    async def _ask_tool_permission(self: GraphPermissionHost, tool_calls: list[dict]) -> str | None:
        tool_list = ", ".join(t["name"] for t in tool_calls)
        choices = [
            ("Yes, always", "a", "Allow these tools for this session"),
            ("Yes", "y", "Allow this tool use once"),
            ("No", "n", "Deny these tools"),
        ]
        details = [item.model_dump() for item in self._permission_tool_details(tool_calls)]

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

    def _permission_tool_details(self: GraphPermissionHost, tool_calls: list[dict]) -> list[PermissionToolDetail]:
        details: list[PermissionToolDetail] = []
        for call in tool_calls:
            classified = classify_tool_call(call)
            details.append(PermissionToolDetail(
                name=classified.name,
                pattern=build_pattern(classified.name, classified.args),
                args=classified.args,
            ))
        return details


def _active_workflow_names(value: object) -> list[str]:
    names: list[str] = []
    items = value.values() if isinstance(value, dict) else value or []
    for item in items:
        try:
            run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
        except (TypeError, ValueError):
            continue
        if run.status == WorkflowRunStatus.ACTIVE and run.name.strip():
            names.append(run.name.strip())
    return names


def _gate_denial_reason(tool_name: str, active_workflows: list[str]) -> str:
    blockers: list[str] = []
    for workflow in active_workflows:
        gate = workflow_gate(workflow)
        if gate and tool_name in gate.denied_tools:
            requirement = gate.required_before_transition or gate.description
            blockers.append(f"{workflow}: {requirement}")
    details = "; ".join(blockers) if blockers else "active workflow gate"
    return f"Blocked by workflow gate for tool '{tool_name}': {details}"


def _persona_requires_approval(capability: PermissionCapability, runtime_persona: str) -> bool:
    if capability not in {PermissionCapability.FILE_WRITE, PermissionCapability.FILE_FORMAT}:
        return False
    personas = {
        item.strip()
        for item in (runtime_persona or "coordinate").split(",")
        if item.strip()
    }
    return "implement" not in personas
