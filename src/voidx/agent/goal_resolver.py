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
            runnable.ainvoke(_resolver_messages_from_exchanges(user_text, task_state)),
            timeout=GOAL_RESOLVER_TIMEOUT_SECONDS,
        )
        resolution = _coerce_resolution(raw)
    except Exception:
        resolution = None

    if resolution is None:
        return _normalize_resolution(fallback, user_text, interaction_mode, task_state)
    return _normalize_resolution(resolution, user_text, interaction_mode, task_state)


def _resolver_messages_from_exchanges(user_text: str, task_state: TaskState) -> list:
    messages = [SystemMessage(content=_resolver_system_prompt(task_state))]
    for exchange in task_state.recent_exchanges:
        if exchange.user_text:
            messages.append(HumanMessage(content=exchange.user_text))
        if exchange.assistant_text:
            messages.append(AIMessage(content=exchange.assistant_text))
    messages.append(HumanMessage(content=user_text))
    return messages


def _resolver_system_prompt(task_state: TaskState) -> str:
    available_joins = ", ".join(sorted(_ALLOWED_JOIN_NODES))
    lines = [
        "You are resolving the user's intent and goal for this turn.",
        "Return structured data matching the GoalResolution schema.",
        "",
        "Rules:\n"
        "- intent.type=general only for non-code, non-workspace conversation.\n"
        "- intent.type=coding for codebase inspection, design, docs, review, debugging, or edits.\n"
        "- Pick exactly one goal.type when intent is coding and a concrete workspace goal exists.\n"
        "- plan.join is the workflow node to enter. Required when goal is set; null when goal is null.\n"
        "- plan.leave is the workflow node after which automatic progression stops. Optional.\n"
        f"- Available join values: {available_joins}.\n"
        "- If intent does not clearly match any join value, set goal=null and plan=null.\n"
        "- If the user message is a short continuation (e.g. ok, continue, go on, 改) and there is an active workflow, set intent=coding, keep the current goal, and set plan.join to the active workflow name.\n"
        "- goal and plan are bound: if goal is set, plan must be set with join; if goal is null, plan must be null.\n"
    ]
    if task_state.current_goal is not None:
        active = _current_active_join(task_state)
        lines.extend([
            "",
            "Current state:",
            f"- intent: {task_state.current_intent.value}",
            f"- goal: {task_state.current_goal.type.value} — {task_state.current_goal.label}",
        ])
        if active:
            lines.append(f"- active workflows: {active}")
    return "\n".join(lines)


_ALLOWED_JOIN_NODES = {"debug", "brainstorm", "design", "plan", "tdd", "review", "feedback"}


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

    # general intent with active workflow: preserve current workflow
    if resolution.intent.type == TaskIntent.GENERAL:
        current_join = _current_active_join(task_state)
        if current_join and task_state.current_goal is not None:
            return GoalResolution(
                intent=IntentResolution(type=TaskIntent.CODING, desc="continuation of active workflow"),
                goal=task_state.current_goal,
                plan=PlanResolution(join=current_join, leave=None),
            )
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


def _current_active_join(task_state: TaskState) -> str:
    if task_state.workflow_route is not None and task_state.workflow_route.join:
        return task_state.workflow_route.join
    for name, run in task_state.workflow_runs.items():
        if getattr(run.status, "value", run.status) == "active":
            return name
    return ""
