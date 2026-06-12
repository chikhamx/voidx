"""Runtime-owned structured goal resolution for top-level turns."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from voidx.agent.runtime_context import InteractionMode, TaskIntent
from voidx.agent.task_state import Goal, GoalResolution, GoalType, TaskState, goal_from_text, resolve_turn_intent


GOAL_RESOLVER_TIMEOUT_SECONDS = 20


async def resolve_goal_for_turn(
    *,
    model: Any | None,
    user_text: str,
    interaction_mode: str | InteractionMode | None,
    task_state: TaskState,
    workspace: str,
    session_time: str,
    title_requested: bool = False,
) -> GoalResolution:
    fallback = resolve_turn_intent(user_text, interaction_mode, task_state)
    if fallback.confirmed_approval is not None:
        return fallback
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
                title_requested=title_requested,
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
        title_requested=title_requested,
    )


def _resolver_messages(
    user_text: str,
    interaction_mode: str | InteractionMode | None,
    task_state: TaskState,
    workspace: str,
    session_time: str,
    *,
    title_requested: bool = False,
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
        "title_requested": title_requested,
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
        "- If pending_approval is present and the user clearly approves it, use that scope as the goal target and set user_requested_write=true.\n"
        "- In plan mode, return a design goal with needs_confirmation=true.\n"
        "- If title_requested=true, optionally set title to a concise session title. Return null or empty when the user text is too short or unclear.\n"
        "- If title_requested=false, leave title null.\n\n"
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
    title_requested: bool = False,
) -> GoalResolution:
    mode = InteractionMode.parse(interaction_mode)
    confidence = resolution.confidence
    reason = resolution.reason.strip() or "structured goal resolver"
    title = _normalize_title(resolution.title) if title_requested else None

    fallback = resolve_turn_intent(user_text, interaction_mode, task_state)
    if fallback.confirmed_approval is not None:
        return fallback.model_copy(update={"title": title})

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
            title=title,
        )

    if resolution.intent == TaskIntent.GENERAL:
        return GoalResolution(
            intent=TaskIntent.GENERAL,
            goal=None,
            confidence=confidence,
            reason=reason,
            title=title,
        )

    goal = resolution.goal
    if mode == InteractionMode.GOAL and task_state.current_goal is not None and goal is not None:
        goal = goal.model_copy(update={"target": task_state.current_goal.label})

    return GoalResolution(
        intent=TaskIntent.CODING,
        goal=goal,
        confidence=confidence,
        reason=reason,
        confirmed_approval=resolution.confirmed_approval,
        title=title,
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


def _normalize_title(value: str | None) -> str | None:
    title = (value or "").strip().strip("\"'").strip()
    if not title or "```" in title:
        return None
    return title[:60].rstrip() or None
