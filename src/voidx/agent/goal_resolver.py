"""Runtime-owned structured goal resolution for top-level turns."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError as PydanticValidationError

from voidx.logging.request_log import log_llm_diagnostic, serialize_llm_message
from voidx.runtime.intent import InteractionMode, TaskIntent, _contains_any, infer_task_intent
from voidx.runtime.task_state import (
    GoalResolution,
    GoalSpec,
    GoalType,
    IntentResolution,
    PlanResolution,
    TaskState,
    infer_goal_type,
)
from voidx.workflow.dag import DEFAULT_WORKFLOW_DAG


GOAL_RESOLVER_TIMEOUT_SECONDS = 20


def resolve_plan_mode(user_text: str, task_state: TaskState) -> GoalResolution:
    """PLAN mode: construct result directly without LLM call."""
    desc = (
        task_state.current_goal.desc
        if task_state.current_goal and task_state.current_goal.desc.strip()
        else user_text
    )
    return GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING, desc="plan mode"),
        goal=GoalSpec(type=GoalType.DESIGN, desc=desc),
        plan=PlanResolution(join="brainstorm", leave="brainstorm"),
    )


def resolve_goal_mode(user_text: str, task_state: TaskState) -> GoalResolution:
    """GOAL mode: construct result directly without LLM call.

    The user must specify a goal to enter goal mode. Fixed entry from plan node;
    plan will clarify via questions before planning.
    """
    goal = task_state.current_goal or GoalSpec(type=GoalType.FEATURE, desc=user_text)
    return GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING, desc="goal mode"),
        goal=goal,
        plan=PlanResolution(join="plan", leave=None),
    )


async def resolve_goal_for_turn(
    *,
    model: Any | None,
    user_text: str,
    interaction_mode: str | InteractionMode | None,
    task_state: TaskState,
    log_diagnostic: bool = True,
) -> GoalResolution:
    fallback = GoalResolution(
        intent=IntentResolution(type=TaskIntent.GENERAL, desc=""),
        goal=None,
        plan=None,
    )
    fallback_reason = ""
    fallback_error_type = ""
    fallback_error = ""
    fallback_is_validation_error = False
    if model is None:
        fallback_reason = "model_unavailable"
        normalized = _normalize_resolution(fallback, user_text, interaction_mode, task_state)
        _log_goal_resolver_decision(normalized, user_text, task_state, fallback_reason, fallback_error_type, fallback_error, enabled=log_diagnostic)
        return normalized

    structured = getattr(model, "with_structured_output", None)
    if not callable(structured):
        fallback_reason = "structured_output_unsupported"
        normalized = _normalize_resolution(fallback, user_text, interaction_mode, task_state)
        _log_goal_resolver_decision(normalized, user_text, task_state, fallback_reason, fallback_error_type, fallback_error, enabled=log_diagnostic)
        return normalized

    try:
        runnable = structured(GoalResolution)
        resolver_messages = _resolver_messages_from_exchanges(user_text, task_state)
        raw = await asyncio.wait_for(
            runnable.ainvoke(resolver_messages),
            timeout=GOAL_RESOLVER_TIMEOUT_SECONDS,
        )
        _log_goal_resolver_exchange(resolver_messages, raw=raw, enabled=log_diagnostic)
        resolution = _coerce_resolution(raw)
    except Exception as exc:
        fallback_reason = "structured_output_error"
        fallback_error_type = type(exc).__name__
        fallback_error = _truncate_error_text(str(exc))
        fallback_is_validation_error = isinstance(exc, PydanticValidationError)
        if "resolver_messages" in locals():
            _log_goal_resolver_exchange(
                resolver_messages,
                error_type=fallback_error_type,
                error=fallback_error,
                enabled=log_diagnostic,
            )
        resolution = None

    if resolution is None:
        fallback_reason = fallback_reason or "invalid_structured_output"
        fallback_resolution = (
            _local_coding_fallback(user_text, interaction_mode)
            if fallback_is_validation_error
            else fallback
        )
        normalized = _normalize_resolution(fallback_resolution, user_text, interaction_mode, task_state)
    else:
        normalized = _normalize_resolution(resolution, user_text, interaction_mode, task_state)
    _log_goal_resolver_decision(normalized, user_text, task_state, fallback_reason, fallback_error_type, fallback_error, enabled=log_diagnostic)
    return normalized


def _resolver_messages_from_exchanges(user_text: str, task_state: TaskState) -> list:
    messages = [SystemMessage(content=_resolver_system_prompt(task_state))]
    for exchange in task_state.recent_exchanges:
        if exchange.user_text:
            messages.append(HumanMessage(content=exchange.user_text))
        if exchange.assistant_text:
            messages.append(AIMessage(content=exchange.assistant_text))
    messages.append(HumanMessage(content=user_text))
    return messages


def _log_goal_resolver_decision(
    resolution: GoalResolution,
    user_text: str,
    task_state: TaskState,
    fallback_reason: str,
    fallback_error_type: str,
    fallback_error: str,
    *,
    enabled: bool = True,
) -> None:
    goal = resolution.goal
    plan = resolution.plan
    log_llm_diagnostic(
        "goal_resolver_decision",
        enabled=enabled,
        intent=resolution.intent.type.value,
        goal_type=goal.type.value if goal is not None else "",
        goal_desc=goal.desc if goal is not None else "",
        plan_join=plan.join if plan is not None else "",
        plan_leave=plan.leave if plan is not None and plan.leave is not None else "",
        fallback_reason=fallback_reason,
        fallback_error_type=fallback_error_type,
        fallback_error=fallback_error,
        active_workflows=_active_workflow_names(task_state),
        user_text=user_text,
    )


def _log_goal_resolver_exchange(
    messages: list[BaseMessage],
    *,
    raw: object | None = None,
    error_type: str = "",
    error: str = "",
    enabled: bool = True,
) -> None:
    response: dict[str, Any] = {}
    if error_type or error:
        response["error_type"] = error_type
        response["error"] = error
    else:
        response["raw"] = _raw_response_for_log(raw)
    log_llm_diagnostic(
        "goal_resolver_exchange",
        enabled=enabled,
        request={"messages": [serialize_llm_message(message) for message in messages]},
        response=response,
    )


def _raw_response_for_log(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, AIMessage):
        return {
            "content": value.content,
            "tool_calls": getattr(value, "tool_calls", None) or [],
            "usage_metadata": getattr(value, "usage_metadata", None) or {},
        }
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (dict, list, tuple)):
        return value
    return repr(value)


def _local_coding_fallback(
    user_text: str,
    interaction_mode: str | InteractionMode | None,
) -> GoalResolution:
    intent = infer_task_intent(user_text, interaction_mode)
    if intent == TaskIntent.GENERAL:
        return GoalResolution(
            intent=IntentResolution(type=TaskIntent.GENERAL, desc="local fallback classified as general"),
            goal=None,
            plan=None,
        )
    goal_type = infer_goal_type(user_text)
    return GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING, desc="local fallback after resolver validation error"),
        goal=GoalSpec(type=goal_type, desc=user_text),
        plan=_fallback_plan_for_text(user_text),
    )


def _truncate_error_text(value: str, limit: int = 2000) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _resolver_system_prompt(task_state: TaskState) -> str:
    available_joins = ", ".join(sorted(_ALLOWED_JOIN_NODES))
    goal_types = ", ".join(item.value for item in GoalType)
    lines = [
        "You are resolving the user's intent and goal for this turn.",
        "Return structured data matching the GoalResolution schema.",
        "",
        "GoalResolution schema:",
        "- intent: {type: 'coding' | 'general', desc: string}",
        f"- goal: null or {{type: one of [{goal_types}], desc: string}}",
        f"- plan: null or {{join: one of [{available_joins}], leave: null or workflow node name}}",
        "",
        "Available join values:",
        "- brainstorm: Confirm requirements and design, get user approval",
        "- design: Produce a structured document that passes the reader test",
        "- plan: Produce an executable implementation plan, get user approval",
        "- tdd: Complete implementation via TDD cycle, all tests green",
        "- verify: Prove changes reach expected state with reproducible evidence",
        "- review: Initiate structured code review request and collect verdict",
        "- feedback: Verify and implement valid review feedback",
        "- debug: Locate root cause and confirm fix direction",
        "",
        "Rules:\n"
        "- intent.type=general only for non-code, non-workspace conversation.\n"
        "- intent.type=coding for codebase inspection, design, docs, review, debugging, or edits.\n"
        "- Pick exactly one goal.type when intent is coding and a concrete workspace goal exists.\n"
        "- plan.join is the workflow node to enter. Required when goal is set; null when goal is null.\n"
        "- plan.leave is the workflow node after which automatic progression stops. Optional.\n"
        "- If intent does not clearly match any join value, set goal=null and plan=null.\n"
        "- If the user message is a short continuation (e.g. ok, continue, go on, 改) and there is an active workflow, set intent=coding, keep the current goal, and set plan.join to the active workflow name.\n"
        "- goal and plan are bound: if goal is set, plan must be set with join; if goal is null, plan must be null.\n"
        "- goal.desc: a short summary of the user's request in their language (1-2 sentences).\n"
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
    log_diagnostic: bool = True,
) -> GoalResolution:
    mode = InteractionMode.parse(interaction_mode)

    # Validate join against resolver entry points, and leave against DAG nodes.
    plan = resolution.plan
    if plan is not None:
        if plan.join and plan.join not in _ALLOWED_JOIN_NODES:
            plan = None
        elif plan.leave and plan.leave not in DEFAULT_WORKFLOW_DAG.nodes:
            plan = PlanResolution(join=plan.join, leave=None)

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

    # coding intent: require explicit plan routing instead of deriving workflow
    # from goal.type. GoalType is semantic; plan.join/leave are routing.
    goal = resolution.goal
    if goal is not None and (plan is None or not plan.join):
        goal = None
        plan = None

    return GoalResolution(
        intent=resolution.intent,
        goal=goal,
        plan=plan,
    )


def _fallback_plan_for_text(user_text: str) -> PlanResolution:
    normalized = user_text.lower()
    if _contains_any(normalized, ("review", "code review", "审查", "复核", "评审")):
        return PlanResolution(join="review", leave="review")
    if _contains_any(normalized, (
        "debug",
        "traceback",
        "stacktrace",
        "bug",
        "failing",
        "failure",
        "failed",
        "报错",
        "排查",
        "调试",
        "异常",
        "故障",
        "错误",
        "问题",
    )):
        return PlanResolution(join="debug", leave="verify")
    if _contains_any(normalized, ("doc", "docs", "readme", "spec", "文档", "规格", "说明")):
        return PlanResolution(join="design", leave="design")
    if _contains_any(normalized, (
        "implement",
        "apply",
        "change",
        "edit",
        "fix",
        "modify",
        "patch",
        "refactor",
        "write",
        "改",
        "修",
        "修复",
        "修改",
        "实现",
        "落地",
    )):
        return PlanResolution(join="tdd", leave="verify")
    return PlanResolution(join="brainstorm", leave="brainstorm")


def _current_active_join(task_state: TaskState) -> str:
    if task_state.workflow_route is not None and task_state.workflow_route.join:
        return task_state.workflow_route.join
    for name, run in task_state.workflow_runs.items():
        if getattr(run.status, "value", run.status) == "active":
            return name
    return ""


def _active_workflow_names(task_state: TaskState) -> list[str]:
    return [
        name
        for name, run in task_state.workflow_runs.items()
        if getattr(run.status, "value", run.status) == "active"
    ]
