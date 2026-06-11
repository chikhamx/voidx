"""Runtime-owned intent refinement decisions."""

from __future__ import annotations

from voidx.agent.agents import get_agent
from voidx.agent.runtime_context import InteractionMode, TaskIntent
from voidx.agent.task_state import PendingApproval, TaskPhase, ToolStatePatch
from voidx.config import Config, Settings
from voidx.tools.base import ToolContext
from voidx.tools.on_intent import OnIntentInput, OnIntentResult
from voidx.workflow.runtime import (
    WorkflowRunState,
    WorkflowRunStatus,
    WorkflowStateEvent,
    WorkflowStateEventKind,
    advance_workflow_states,
)
from voidx.workflow.service import WorkflowMatch, WorkflowService


def refine_intent(
    inp: OnIntentInput,
    ctx: ToolContext,
    *,
    config: Config,
    settings: Settings | None,
    registered_tool_ids: list[str],
) -> OnIntentResult:
    mode = InteractionMode.parse(ctx.interaction_mode)
    confirmed, reason, needs_confirmation = _confirm_intent(inp, ctx, mode)
    phase = _phase_for_intent(confirmed)
    can_attempt_implementation = (
        confirmed == TaskIntent.IMPLEMENT
        and mode != InteractionMode.PLAN
        and not needs_confirmation
    )
    available_tool_ids = _available_tools_for_intent(
        confirmed,
        agent=ctx.agent,
        interaction_mode=mode,
        registered_tool_ids=registered_tool_ids,
        can_attempt_implementation=can_attempt_implementation,
    )
    matches = _workflow_matches(
        inp,
        confirmed,
        ctx,
        phase=phase,
        config=config,
        settings=settings,
    )
    workflow_runs = _reconciled_workflow_runs(
        matches,
        confirmed=confirmed,
        phase=phase,
        scope=inp.scope,
        ctx=ctx,
    )
    active_workflow_runs = [
        run for run in workflow_runs
        if run.status == WorkflowRunStatus.ACTIVE
    ]

    pending_approval = _pending_approval_for_intent(
        confirmed,
        inp.scope.strip(),
        ctx.goal_turn_count,
    )
    patch = ToolStatePatch(
        task_intent=confirmed,
        intent_resolution_reason=f"on_intent: {reason}",
        goal_phase=phase,
        pending_approval=pending_approval,
        available_tool_ids=available_tool_ids,
        workflow_runs=workflow_runs,
        intent_confidence=inp.confidence,
        intent_source="on_intent",
        intent_refined=True,
    )

    return OnIntentResult(
        confirmed_intent=confirmed,
        confidence=inp.confidence,
        reason=reason,
        phase=phase,
        active_workflow_runs=active_workflow_runs,
        available_tool_ids=available_tool_ids,
        needs_user_confirmation=needs_confirmation,
        state_patch=patch,
    )


def _confirm_intent(
    inp: OnIntentInput,
    ctx: ToolContext,
    mode: InteractionMode,
) -> tuple[TaskIntent, str, bool]:
    proposed = TaskIntent(inp.intent)
    reason = inp.reason.strip() or f"model selected {proposed.value}"

    if mode == InteractionMode.PLAN and proposed == TaskIntent.IMPLEMENT:
        return (
            TaskIntent.DESIGN,
            f"{reason}; plan mode blocks implementation, so runtime kept this as design",
            False,
        )

    if proposed == TaskIntent.IMPLEMENT and inp.confidence < 0.65 and not ctx.pending_approval:
        return (
            TaskIntent.DESIGN,
            f"{reason}; implementation confidence is below 0.65, so runtime requires confirmation",
            True,
        )

    return proposed, reason, False


def _available_tools_for_intent(
    intent: TaskIntent,
    *,
    agent: str,
    interaction_mode: InteractionMode,
    registered_tool_ids: list[str],
    can_attempt_implementation: bool,
) -> list[str]:
    registered = set(registered_tool_ids)
    agent_def = get_agent(agent)
    agent_tools = set(agent_def.tools if agent_def else registered)

    read_tools = {
        "on_intent",
        "advance_workflow",
        "read",
        "glob",
        "grep",
        "webfetch",
        "websearch",
        "repo_map",
        "lsp_diagnostics",
        "lsp_symbols",
        "lsp_definition",
        "lsp_references",
        "task_status",
        "load_skills",
    }
    planning_tools = read_tools | {"agent", "todo", "bash"}
    implementation_tools = set(agent_tools)
    review_tools = read_tools | {"agent", "todo", "bash"}

    if intent in {TaskIntent.CHAT, TaskIntent.AMBIGUOUS}:
        desired = {"load_skills"}
    elif intent == TaskIntent.INSPECT:
        desired = read_tools
    elif intent == TaskIntent.DESIGN:
        desired = planning_tools
    elif intent == TaskIntent.REVIEW:
        desired = review_tools
    elif intent == TaskIntent.DEBUG:
        desired = review_tools
    elif intent == TaskIntent.IMPLEMENT and can_attempt_implementation:
        desired = implementation_tools
    else:
        desired = planning_tools

    if interaction_mode == InteractionMode.PLAN:
        desired = desired - {"write", "edit", "lsp_format"}

    allowed = desired & agent_tools & registered
    return [tool for tool in _ordered_agent_tools(agent, registered_tool_ids) if tool in allowed]


def _pending_approval_for_intent(
    intent: TaskIntent,
    scope: str,
    turn_count: int,
) -> PendingApproval | None:
    if intent != TaskIntent.DESIGN:
        return None
    return PendingApproval(
        scope=scope,
        source_intent=TaskIntent.DESIGN,
        created_turn=turn_count,
    )


def _ordered_agent_tools(agent: str, registered_tool_ids: list[str]) -> list[str]:
    agent_def = get_agent(agent)
    if not agent_def:
        return registered_tool_ids
    registered = set(registered_tool_ids)
    ordered = [tool for tool in agent_def.tools if tool in registered]
    for tool in registered_tool_ids:
        if tool not in ordered and tool in registered:
            ordered.append(tool)
    return ordered


def _workflow_matches(
    inp: OnIntentInput,
    intent: TaskIntent,
    ctx: ToolContext,
    *,
    phase: str,
    config: Config,
    settings: Settings | None,
) -> list[WorkflowMatch]:
    del config, settings
    service = WorkflowService()
    text = inp.scope or inp.reason
    matches = service.select(
        text,
        agent=ctx.agent,
        task_intent=intent.value,
        interaction_mode=ctx.interaction_mode,
    )
    seen = {_normalize_name(match.name) for match in matches}
    for name in inp.suggested_workflows:
        normalized = _normalize_name(name)
        if normalized in seen:
            continue
        node = service.get(normalized)
        if node is None or not node.enabled:
            continue
        matches.append(WorkflowMatch(node=node, reason="suggested"))
        seen.add(normalized)
    return matches


def _reconciled_workflow_runs(
    matches: list[WorkflowMatch],
    *,
    confirmed: TaskIntent,
    phase: str,
    scope: str,
    ctx: ToolContext,
) -> list[WorkflowRunState]:
    current = _current_workflow_runs(ctx)
    desired_names = {_normalize_name(match.name) for match in matches}
    current_by_name = {_normalize_name(run.name): run for run in current}
    events = [
        WorkflowStateEvent(
            workflow=run.name,
            kind=WorkflowStateEventKind.SKIPPED,
            ref="tool:on_intent",
            ok=True,
            summary=f"Workflow no longer matches refined intent {confirmed.value}.",
            reason=f"intent refined to {confirmed.value}",
        )
        for run in current
        if run.status == WorkflowRunStatus.ACTIVE
        and _normalize_name(run.name) not in desired_names
    ]
    reconciled = (
        advance_workflow_states(current, events, turn_count=ctx.goal_turn_count)
        if events
        else current
    )
    additions = [
        WorkflowRunState.from_match(
            match,
            phase=phase,
            scope=scope,
            turn_count=ctx.goal_turn_count,
        )
        for match in matches
        if current_by_name.get(_normalize_name(match.name)) is None
        or current_by_name[_normalize_name(match.name)].status != WorkflowRunStatus.ACTIVE
    ]
    return [*reconciled, *additions]


def _current_workflow_runs(ctx: ToolContext) -> list[WorkflowRunState]:
    runs: list[WorkflowRunState] = []
    for item in ctx.workflow_runs:
        try:
            run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
        except (TypeError, ValueError):
            continue
        if _normalize_name(run.name):
            runs.append(run.model_copy(deep=True))
    return runs


def _normalize_name(name: str) -> str:
    return name.strip().lower()


def _phase_for_intent(intent: TaskIntent) -> str:
    if intent == TaskIntent.INSPECT:
        return TaskPhase.INSPECT.value
    if intent == TaskIntent.DESIGN:
        return TaskPhase.DESIGN.value
    if intent == TaskIntent.IMPLEMENT:
        return TaskPhase.IMPLEMENT.value
    if intent == TaskIntent.REVIEW:
        return TaskPhase.REVIEW.value
    if intent == TaskIntent.DEBUG:
        return TaskPhase.INSPECT.value
    return TaskPhase.CLARIFY.value
