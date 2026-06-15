"""Runtime-owned structured goal resolution for top-level turns."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from voidx.agent.runtime_context import InteractionMode, TaskIntent
from voidx.agent.task_state import (
    Goal,
    GoalResolution,
    GoalType,
    TaskState,
    default_workflow_end_for_goal,
    goal_from_text,
    resolve_turn_intent,
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
    fallback = resolve_turn_intent(user_text, interaction_mode, task_state)
    if model is None:
        return fallback

    structured = getattr(model, "with_structured_output", None)
    if not callable(structured):
        return fallback

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
        return fallback

    if resolution is None:
        return fallback
    return _normalize_resolution(
        resolution,
        user_text=user_text,
        interaction_mode=interaction_mode,
        task_state=task_state,
    )


def _resolver_messages(
    user_text: str,
    interaction_mode: str | InteractionMode | None,
    task_state: TaskState,
    workspace: str,
    session_time: str,
) -> list:
    schema = json.dumps(GoalResolution.model_json_schema(), ensure_ascii=False)
    pending = (
        task_state.pending_approval.model_dump(mode="json")
        if task_state.pending_approval is not None
        else None
    )
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
        "pending_approval": pending,
        "recent_user_texts": task_state.recent_user_texts[-2:],
        "latest_user_text": user_text,
    }
    system = (
        "You are voidx resolving the current user's goal before normal work begins.\n"
        "Return only structured data matching the GoalResolution schema.\n\n"
        "Rules:\n"
        "- Use intent=general only for non-code, non-workspace conversation.\n"
        "- Use intent=coding for codebase inspection, design, docs, review, debugging, or edits.\n"
        "- Pick exactly one GoalType when intent=coding and a concrete workspace goal exists.\n"
        "- Do not infer write permission from analysis words like look at, inspect, 看看, 分析, or 建议.\n"
        "- Set user_requested_write=true only when the user explicitly asks to change, fix, implement, edit, write, apply, or continue an approved implementation.\n"
        "- Set needs_confirmation=true when approval/write intent is ambiguous.\n"
        "- Set workflow_start to the first workflow node that should run for this turn.\n"
        "- Set workflow_end to the workflow node where automatic workflow progression must stop.\n"
        "- Use workflow_start=review and workflow_end=review for review-only requests.\n"
        "- Use workflow_start=review and workflow_end=verify only when the user explicitly asks to fix, implement, apply, or continue after review findings.\n"
        "- If the user explicitly asks to implement an already detailed spec, set workflow_start=tdd and workflow_end=verify.\n"
        "- If the user asks to turn a spec into an implementation plan, set workflow_start=plan.\n"
        "- If the user asks to write or revise a design/spec document, set workflow_start=design-doc.\n"
        "- Do not choose brainstorm when the request already contains an approved or sufficiently detailed spec.\n"
        "- Do not set workflow_start or workflow_end based on vague or ambiguous approval.\n"
        "- In plan mode, return a design goal with needs_confirmation=true.\n"
        f"GoalResolution JSON schema:\n{schema}"
    )
    return [
        SystemMessage(content=system),
        HumanMessage(content=json.dumps(context, ensure_ascii=False, indent=2)),
    ]


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
    *,
    user_text: str,
    interaction_mode: str | InteractionMode | None,
    task_state: TaskState,
) -> GoalResolution:
    mode = InteractionMode.parse(interaction_mode)
    confidence = resolution.confidence
    reason = resolution.reason.strip() or "structured goal resolver"
    workflow_start = _normalize_workflow_route_node(resolution.workflow_start)
    workflow_end = _normalize_workflow_route_node(resolution.workflow_end)
    if workflow_start and not workflow_end:
        workflow_end = default_workflow_end_for_goal(resolution.goal, workflow_start)

    if mode == InteractionMode.PLAN:
        goal = _copy_goal(
            resolution.goal,
            fallback_text=user_text,
            goal_type=GoalType.DESIGN,
            user_requested_write=False,
            needs_confirmation=True,
        )
        return GoalResolution(
            intent=TaskIntent.CODING,
            goal=goal,
            confidence=confidence,
            reason=f"{reason}; plan mode forces design goal",
            workflow_start=workflow_start,
            workflow_end=workflow_end,
        )

    if resolution.intent == TaskIntent.GENERAL:
        return GoalResolution(
            intent=TaskIntent.GENERAL,
            goal=None,
            confidence=confidence,
            reason=reason,
            workflow_start=workflow_start,
            workflow_end=workflow_end,
        )

    goal = resolution.goal
    if mode == InteractionMode.GOAL and task_state.current_goal is not None and goal is not None:
        goal = goal.model_copy(update={"target": task_state.current_goal.label})

    return GoalResolution(
        intent=TaskIntent.CODING,
        goal=goal,
        confidence=confidence,
        reason=reason,
        workflow_start=workflow_start,
        workflow_end=workflow_end,
    )


def _copy_goal(
    goal: Goal | None,
    *,
    fallback_text: str,
    goal_type: GoalType,
    user_requested_write: bool,
    needs_confirmation: bool,
) -> Goal:
    target = goal.target if goal is not None and goal.target.strip() else fallback_text
    expected_result = goal.expected_result if goal is not None else ""
    return goal_from_text(
        target,
        goal_type=goal_type,
        user_requested_write=user_requested_write,
        needs_confirmation=needs_confirmation,
        expected_result=expected_result,
    )


def _normalize_workflow_route_node(value: str | None) -> str | None:
    workflow = (value or "").strip().lower()
    if not workflow:
        return None
    if workflow not in DEFAULT_WORKFLOW_DAG.nodes:
        return None
    return workflow
