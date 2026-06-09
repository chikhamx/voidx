"""Runtime-owned intent refinement decisions."""

from __future__ import annotations

from voidx.agent.agents import get_agent
from voidx.agent.runtime_context import InteractionMode, TaskIntent
from voidx.agent.task_state import PendingApproval, TaskPhase, ToolStatePatch
from voidx.config import Config, Settings
from voidx.skills.registry import SkillRegistry, normalize_skill_name
from voidx.skills.runtime import SkillRunState
from voidx.skills.schema import SkillMatch
from voidx.skills.service import SkillService
from voidx.tools.base import ToolContext
from voidx.tools.on_intent import OnIntentInput, OnIntentResult


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
    matches = _skill_matches(
        inp,
        confirmed,
        ctx,
        phase=phase,
        config=config,
        settings=settings,
    )
    skill_runs = [
        SkillRunState.from_match(
            match,
            phase=phase,
            scope=inp.scope,
            turn_count=ctx.goal_turn_count,
        )
        for match in matches
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
        skill_runs=skill_runs,
        intent_confidence=inp.confidence,
        intent_source="on_intent",
        intent_refined=True,
    )

    return OnIntentResult(
        confirmed_intent=confirmed,
        confidence=inp.confidence,
        reason=reason,
        phase=phase,
        active_skill_runs=skill_runs,
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


def _skill_matches(
    inp: OnIntentInput,
    intent: TaskIntent,
    ctx: ToolContext,
    *,
    phase: str,
    config: Config,
    settings: Settings | None,
) -> list[SkillMatch]:
    service = _skill_service(config, settings)
    text = inp.scope or inp.reason
    matches = service.select(
        text,
        agent=ctx.agent,
        task_intent=intent.value,
        interaction_mode=ctx.interaction_mode,
        scopes=("bundled",),
        exclude_names=ctx.active_skill_names,
    )
    seen = {normalize_skill_name(match.name) for match in matches}
    excluded = {normalize_skill_name(name) for name in ctx.active_skill_names}
    for name in inp.suggested_skills:
        normalized = normalize_skill_name(name)
        if normalized in seen or normalized in excluded:
            continue
        skill = service.get(normalized)
        if skill is None or skill.meta.scope != "bundled" or not service.is_enabled(skill):
            continue
        matches.append(SkillMatch(skill=skill, reason="suggested"))
        seen.add(normalized)
    return matches


def _skill_service(config: Config, settings: Settings | None) -> SkillService:
    selection = settings.get_skill_selection() if settings is not None else None
    return SkillService(
        SkillRegistry(config.workspace),
        selection=selection,
    )


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
