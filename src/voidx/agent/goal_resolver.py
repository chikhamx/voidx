"""Runtime-owned structured goal resolution for top-level turns."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, model_validator

from voidx.logging.request_log import log_llm_diagnostic, serialize_llm_message
from voidx.runtime.intent import InteractionMode, TaskIntent, _contains_any
from voidx.runtime.task_state import (
    GoalResolution,
    GoalSpec,
    IntentResolution,
    PlanResolution,
    TaskState,
    goal_type_from_join,
)
from voidx.workflow.dag import DEFAULT_WORKFLOW_DAG
from voidx.llm.service import DeepSeekChatOpenAI


GOAL_RESOLVER_TIMEOUT_SECONDS = 20
WorkflowName = Literal["brainstorm", "debug", "design", "feedback", "plan", "review", "tdd", "verify"]


class ResolverGoal(BaseModel):
    intent: Literal["coding", "general"] = "general"
    goal: str | None = None
    workflow: WorkflowName | None = None
    kind_hint: str | None = None

    @model_validator(mode="after")
    def _goal_and_workflow_are_bound(self) -> "ResolverGoal":
        has_goal = bool(self.goal and self.goal.strip())
        has_workflow = self.workflow is not None
        if has_goal != has_workflow:
            raise ValueError("goal and workflow must be set together")
        if not has_goal:
            self.goal = None
            self.workflow = None
        return self


def resolve_plan_mode(user_text: str, task_state: TaskState) -> GoalResolution:
    """PLAN mode: construct result directly without LLM call."""
    desc = (
        task_state.current_goal.desc
        if task_state.current_goal and task_state.current_goal.desc.strip()
        else user_text
    )
    return GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING),
        goal=GoalSpec(desc=desc),
        plan=PlanResolution(join="brainstorm", leave="brainstorm"),
    )


def resolve_goal_mode(user_text: str, task_state: TaskState) -> GoalResolution:
    """GOAL mode: construct result directly without LLM call.

    The user must specify a goal to enter goal mode. Fixed entry from plan node;
    plan will clarify via questions before planning.
    """
    goal = task_state.current_goal or GoalSpec(desc=user_text)
    return GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING),
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
    del interaction_mode
    fallback = GoalResolution(
        intent=IntentResolution(type=TaskIntent.GENERAL),
        goal=None,
        plan=None,
    )
    fallback_reason = ""
    fallback_error_type = ""
    fallback_error = ""
    if model is None:
        fallback_reason = "model_unavailable"
        normalized = _normalize_resolution(fallback, user_text, task_state)
        _log_goal_resolver_decision(normalized, user_text, task_state, fallback_reason, fallback_error_type, fallback_error, enabled=log_diagnostic)
        return normalized

    structured = getattr(model, "with_structured_output", None)
    if not callable(structured):
        fallback_reason = "structured_output_unsupported"
        normalized = _normalize_resolution(fallback, user_text, task_state)
        _log_goal_resolver_decision(normalized, user_text, task_state, fallback_reason, fallback_error_type, fallback_error, enabled=log_diagnostic)
        return normalized

    resolver_goal: ResolverGoal | None
    try:
        if isinstance(model, DeepSeekChatOpenAI):
            # function_calling sends tool_choice which several providers
            # reject while thinking mode is active.  Fall back to
            # json_mode (response_format {type: json_object}) which
            # does not involve tool_choice.
            method = "json_mode" if model.has_active_reasoning else "function_calling"
        else:
            method = None
        runnable = structured(ResolverGoal) if method is None else structured(ResolverGoal, method=method)
        resolver_messages = _resolver_messages_from_exchanges(user_text, task_state, json_mode=(method == "json_mode"))
        raw = await asyncio.wait_for(
            runnable.ainvoke(resolver_messages),
            timeout=GOAL_RESOLVER_TIMEOUT_SECONDS,
        )
        _log_goal_resolver_exchange(resolver_messages, raw=raw, enabled=log_diagnostic)
        resolver_goal = _coerce_resolution(raw)
    except Exception as exc:
        fallback_reason = "structured_output_error"
        fallback_error_type = type(exc).__name__
        fallback_error = _truncate_error_text(str(exc))
        if "resolver_messages" in locals():
            _log_goal_resolver_exchange(
                resolver_messages,
                error_type=fallback_error_type,
                error=fallback_error,
                enabled=log_diagnostic,
            )
        resolver_goal = None

    if resolver_goal is None:
        fallback_reason = fallback_reason or "invalid_structured_output"
        normalized = _normalize_resolution(fallback, user_text, task_state)
        resolver_kind_hint = ""
    else:
        resolution = _to_goal_resolution(resolver_goal, task_state)
        normalized = _normalize_resolution(resolution, user_text, task_state)
        resolver_kind_hint = resolver_goal.kind_hint or ""
    _log_goal_resolver_decision(
        normalized,
        user_text,
        task_state,
        fallback_reason,
        fallback_error_type,
        fallback_error,
        resolver_kind_hint=resolver_kind_hint,
        enabled=log_diagnostic,
    )
    return normalized


def _resolver_messages_from_exchanges(user_text: str, task_state: TaskState, *, json_mode: bool = False) -> list[BaseMessage]:
    return [
        SystemMessage(content=_resolver_system_prompt(json_mode=json_mode)),
        HumanMessage(content=_resolver_request_markdown(user_text, task_state)),
    ]


def _log_goal_resolver_decision(
    resolution: GoalResolution,
    user_text: str,
    task_state: TaskState,
    fallback_reason: str,
    fallback_error_type: str,
    fallback_error: str,
    *,
    resolver_kind_hint: str = "",
    enabled: bool = True,
) -> None:
    goal = resolution.goal
    plan = resolution.plan
    log_llm_diagnostic(
        "goal_resolver_decision",
        enabled=enabled,
        intent=resolution.intent.type.value,
        goal_type=goal_type_from_join(plan.join if plan is not None else None),
        goal_desc=goal.desc if goal is not None else "",
        plan_join=plan.join if plan is not None else "",
        plan_leave=plan.leave if plan is not None and plan.leave is not None else "",
        resolver_kind_hint=resolver_kind_hint,
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


def _truncate_error_text(value: str, limit: int = 2000) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _resolver_system_prompt(*, json_mode: bool = False) -> str:
    prompt = (
        "You are a goal resolver. Classify the user's current turn into intent, goal, workflow, and kind_hint.\n"
        "\n"
        "## Field Rules\n"
        "\n"
        '- **intent**: "coding" for codebase/workspace work; "general" for non-code conversation.\n'
        "- **goal**: Short user-language summary when a workflow should start; null otherwise. Must be set exactly when workflow is set, and null exactly when workflow is null.\n"
        "- **workflow**: The workflow to start, or null. Must be set exactly when goal is set.\n"
        "- **kind_hint**: Optional semantic hint. Advisory only; never overrides workflow selection.\n"
        "\n"
        "## Available Workflows\n"
        "\n"
        "- brainstorm: Confirm requirements and design, get user approval\n"
        "- debug: Locate root cause and confirm fix direction\n"
        "- design: Produce a structured document that passes the reader test\n"
        "- feedback: Verify and implement valid review feedback\n"
        "- plan: Produce an executable implementation plan, get user approval\n"
        "- review: Initiate structured code review request and collect verdict\n"
        "- tdd: Complete implementation via TDD cycle, all tests green\n"
        "- verify: Prove changes reach expected state with reproducible evidence\n"
    )
    if json_mode:
        prompt += "\nRespond in JSON."
    return prompt


def _resolver_request_markdown(user_text: str, task_state: TaskState) -> str:
    recent_content = _recent_exchanges_content(task_state)
    active = ", ".join(_active_workflow_names(task_state)) or "none"
    goal = task_state.current_goal.label if task_state.current_goal is not None else "none"
    sections = [
        "# Context",
        "",
        f"- intent: {task_state.current_intent.value}",
        f"- goal: {goal}",
        f"- active workflows: {active}",
        "",
        "# Recent Conversation",
        "",
        recent_content,
        "",
        "# Current User Question",
        "",
        user_text,
    ]
    return "\n".join(sections)


_ALLOWED_JOIN_NODES = {"debug", "brainstorm", "design", "plan", "tdd", "review", "feedback", "verify"}


def _coerce_resolution(value: object) -> ResolverGoal | None:
    if isinstance(value, ResolverGoal):
        return value
    if isinstance(value, GoalResolution):
        return _resolver_goal_from_goal_resolution(value)
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
        if "workflow" not in value and ("plan" in value or isinstance(value.get("goal"), dict) or isinstance(value.get("intent"), dict)):
            value = _legacy_dict_to_resolver_dict(value)
        try:
            return ResolverGoal.model_validate(value)
        except ValueError:
            return None
    return None


def _to_goal_resolution(resolver: ResolverGoal, task_state: TaskState) -> GoalResolution:
    del task_state
    intent_type = TaskIntent(resolver.intent)
    if resolver.goal is None or resolver.workflow is None:
        return GoalResolution(intent=IntentResolution(type=intent_type), goal=None, plan=None)
    return GoalResolution(
        intent=IntentResolution(type=intent_type),
        goal=GoalSpec(desc=resolver.goal),
        plan=PlanResolution(join=resolver.workflow, leave=None),
    )


def _resolver_goal_from_goal_resolution(resolution: GoalResolution) -> ResolverGoal | None:
    goal = resolution.goal
    plan = resolution.plan
    return ResolverGoal(
        intent=resolution.intent.type.value,
        goal=goal.desc if goal is not None else None,
        workflow=plan.join if plan is not None else None,
        kind_hint=None,
    )


def _legacy_dict_to_resolver_dict(value: dict) -> dict:
    intent_value = value.get("intent")
    intent = intent_value.get("type") if isinstance(intent_value, dict) else intent_value
    goal_value = value.get("goal")
    if isinstance(goal_value, dict):
        goal = goal_value.get("desc")
        kind_hint = goal_value.get("type")
    else:
        goal = goal_value
        kind_hint = value.get("kind_hint")
    plan_value = value.get("plan")
    workflow = plan_value.get("join") if isinstance(plan_value, dict) else value.get("workflow")
    return {
        "intent": intent or "general",
        "goal": goal,
        "workflow": workflow,
        "kind_hint": kind_hint,
    }


def _normalize_resolution(
    resolution: GoalResolution,
    user_text: str,
    task_state: TaskState,
) -> GoalResolution:
    plan = resolution.plan
    if plan is not None:
        if plan.join and plan.join not in _ALLOWED_JOIN_NODES:
            plan = None
        elif plan.leave and plan.leave not in DEFAULT_WORKFLOW_DAG.nodes:
            plan = PlanResolution(join=plan.join, leave=None)

    if resolution.intent.type == TaskIntent.GENERAL:
        current_join = _current_active_join(task_state)
        if current_join and task_state.current_goal is not None:
            return GoalResolution(
                intent=IntentResolution(type=TaskIntent.CODING),
                goal=task_state.current_goal,
                plan=PlanResolution(join=current_join, leave=None),
            )
        return GoalResolution(
            intent=resolution.intent,
            goal=None,
            plan=None,
        )

    goal = resolution.goal
    if goal is not None and (plan is None or not plan.join):
        current_join = _current_active_join(task_state)
        if current_join and task_state.current_goal is not None and _is_short_continuation(user_text):
            return GoalResolution(
                intent=resolution.intent,
                goal=task_state.current_goal,
                plan=PlanResolution(join=current_join, leave=None),
            )
        goal = None
        plan = None

    return GoalResolution(
        intent=resolution.intent,
        goal=goal,
        plan=plan,
    )


def _recent_exchanges_content(task_state: TaskState) -> str:
    blocks: list[str] = []
    for index, exchange in enumerate(task_state.recent_exchanges, start=1):
        lines: list[str] = [f"## Turn {index}", ""]
        user_text = exchange.user_text.strip()
        assistant_text = exchange.assistant_text.strip()
        if user_text:
            lines.append(f"**Human**: {user_text}")
        if assistant_text:
            lines.append(f"**Assistant**: {assistant_text}")
        if len(lines) <= 2:
            continue
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


_SHORT_CONTINUATION_TEXTS = {"ok", "okay", "continue", "go on", "yes", "y", "改", "继续", "继续改", "好", "好的"}


def _is_short_continuation(user_text: str) -> bool:
    text = user_text.strip().lower()
    if text in _SHORT_CONTINUATION_TEXTS:
        return True
    return len(text) <= 8 and _contains_any(text, ("继续", "改", "ok"))


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
