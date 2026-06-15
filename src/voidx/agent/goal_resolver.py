"""Runtime-owned structured goal resolution for top-level turns."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from voidx.runtime.intent import InteractionMode, TaskIntent
from voidx.runtime.task_state import (
    GoalResolution,
    GoalSpec,
    GoalType,
    IntentResolution,
    PlanResolution,
    TaskState,
    _default_join_for_goal_type,
    _default_leave_for_goal_type,
)
from voidx.workflow.dag import DEFAULT_WORKFLOW_DAG


GOAL_RESOLVER_TIMEOUT_SECONDS = 20


async def resolve_goal_for_turn(
    *,
    model: Any | None,
    user_text: str,
    interaction_mode: str | InteractionMode | None,
    task_state: TaskState,
    workspace: str,
    session_time: str,
) -> GoalResolution:
    fallback = GoalResolution(
        intent=IntentResolution(type=TaskIntent.GENERAL, desc=""),
        goal=None,
        plan=None,
    )
    if model is None:
        return _normalize_resolution(fallback, user_text, interaction_mode, task_state)

    structured = getattr(model, "with_structured_output", None)
    if not callable(structured):
        return _normalize_resolution(fallback, user_text, interaction_mode, task_state)

    try:
        runnable = structured(GoalResolution)
        raw = await asyncio.wait_for(
            runnable.ainvoke(_resolver_messages(
                user_text,
                interaction_mode,
                task_state,
                workspace,
                session_time,
            )),
            timeout=GOAL_RESOLVER_TIMEOUT_SECONDS,
        )
        resolution = _coerce_resolution(raw)
    except Exception:
        resolution = None

    if resolution is None:
        return _normalize_resolution(fallback, user_text, interaction_mode, task_state)
    return _normalize_resolution(resolution, user_text, interaction_mode, task_state)


def _resolver_messages(
    user_text: str,
    interaction_mode: str | InteractionMode | None,
    task_state: TaskState,
    workspace: str,
    session_time: str,
) -> list:
    schema = json.dumps(GoalResolution.model_json_schema(), ensure_ascii=False)
    current_goal = (
        task_state.current_goal.model_dump(mode="json")
        if task_state.current_goal is not None
        else None
    )
    context = {
        "workspace": workspace,
        "session_time": session_time,
        "interaction_mode": InteractionMode.parse(interaction_mode).value,
        "current_intent": task_state.current_intent.value,
        "current_goal": current_goal,

        "recent_user_texts": task_state.recent_user_texts[-2:],
        "latest_user_text": user_text,
    }
    available_joins = ", ".join(sorted(_ALLOWED_JOIN_NODES))
    system = (
        "You are voidx resolving the current user's goal before normal work begins.\n"
        "Return only structured data matching the GoalResolution schema.\n\n"
        "Rules:\n"
        "- intent.type=general only for non-code, non-workspace conversation.\n"
        "- intent.type=coding for codebase inspection, design, docs, review, debugging, or edits.\n"
        "- Pick exactly one goal.type when intent is coding and a concrete workspace goal exists.\n"
        "- plan.join is the workflow node the agent should enter. Required when goal is set; null when goal is null.\n"
        "- plan.leave is the workflow node after which automatic progression stops. Optional.\n"
        f"- Available join values: {available_joins}.\n"
        "- Choose join based on the user's primary intent:\n"
        "  - debug: user reports a bug, error, crash, or unexpected behavior to investigate.\n"
        "  - brainstorm: user wants to explore requirements, design a feature, or discuss approach before coding.\n"
        "  - design-doc: user asks to write or revise a design/spec/PRD/RFC/API doc.\n"
        "  - plan: user asks to turn a spec or requirements into an implementation plan.\n"
        "  - tdd: user explicitly asks to implement an already detailed spec or continue an approved implementation.\n"
        "  - review: user asks for code review or pre-merge review.\n"
        "  - feedback: user provides review feedback or reviewer comments to act on.\n"
        "- If the user's intent does not clearly match any join value, set goal to null and plan to null. The agent will work without workflow constraints.\n"
        "- Do not choose brainstorm when the request already contains an approved or sufficiently detailed spec.\n"
        "- Do not set join or leave based on vague or ambiguous approval.\n"
        "- goal and plan are bound: if goal is set, plan must be set with join; if goal is null, plan must be null.\n"
        f"GoalResolution JSON schema:\n{schema}"
    )
    return [
        SystemMessage(content=system),
        HumanMessage(content=json.dumps(context, ensure_ascii=False, indent=2)),
    ]


_ALLOWED_JOIN_NODES = {"debug", "brainstorm", "design-doc", "plan", "tdd", "review", "feedback"}


def _coerce_resolution(value: object) -> GoalResolution | None:
    if isinstance(value, GoalResolution):
        return value
    if isinstance(value, AIMessage):
        value = value.content
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(value, dict) and "parsed" in value:
        return _coerce_resolution(value.get("parsed"))
    if isinstance(value, dict):
        try:
            return GoalResolution.model_validate(value)
        except ValueError:
            return None
    return None


def _normalize_resolution(
    resolution: GoalResolution,
    user_text: str,
    interaction_mode: str | InteractionMode | None,
    task_state: TaskState,
) -> GoalResolution:
    mode = InteractionMode.parse(interaction_mode)

    # Validate join against resolver entry points, and leave against DAG nodes.
    plan = resolution.plan
    if plan is not None:
        if plan.join and plan.join not in _ALLOWED_JOIN_NODES:
            plan = None
        elif plan.leave and plan.leave not in DEFAULT_WORKFLOW_DAG.nodes:
            plan = PlanResolution(join=plan.join, leave=None)

    # plan mode: force design goal + brainstorm
    if mode == InteractionMode.PLAN:
        desc = (
            resolution.goal.desc
            if resolution.goal is not None and resolution.goal.desc.strip()
            else user_text
        )
        return GoalResolution(
            intent=IntentResolution(type=TaskIntent.CODING, desc="plan mode forces design goal"),
            goal=GoalSpec(type=GoalType.DESIGN, desc=desc),
            plan=PlanResolution(
                join="brainstorm",
                leave=plan.leave if plan is not None else None,
            ),
        )

    # goal mode: keep current_goal unchanged
    if mode == InteractionMode.GOAL and task_state.current_goal is not None:
        current = task_state.current_goal
        goal = GoalSpec(type=current.type, desc=current.desc)
        if plan is None:
            plan = PlanResolution(
                join=_default_join_for_goal_type(current.type),
                leave=_default_leave_for_goal_type(current.type),
            )
        return GoalResolution(
            intent=IntentResolution(
                type=TaskIntent.CODING,
                desc="goal mode keeps the turn scoped to the current goal",
            ),
            goal=goal,
            plan=plan,
        )

    # general intent: no goal, no plan
    if resolution.intent.type == TaskIntent.GENERAL:
        return GoalResolution(
            intent=resolution.intent,
            goal=None,
            plan=None,
        )

    # coding intent: fill default join/leave when needed
    goal = resolution.goal
    if goal is not None:
        if plan is None:
            plan = PlanResolution(
                join=_default_join_for_goal_type(goal.type),
                leave=_default_leave_for_goal_type(goal.type),
            )
        elif not plan.join:
            plan = PlanResolution(
                join=_default_join_for_goal_type(goal.type),
                leave=plan.leave or _default_leave_for_goal_type(goal.type),
            )
        elif not plan.leave:
            plan = PlanResolution(
                join=plan.join,
                leave=_default_leave_for_goal_type(goal.type),
            )

    return GoalResolution(
        intent=resolution.intent,
        goal=goal,
        plan=plan,
    )
