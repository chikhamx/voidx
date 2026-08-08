"""Runtime-owned structured goal resolution for top-level turns."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field, model_validator

from voidx.agent.application.workflow_utils import active_workflow_names
from voidx.logging.request_log import log_llm_diagnostic, serialize_llm_message
from voidx.agent.domain.task.intent import InteractionMode, TaskIntent, _contains_any
from enum import Enum
from voidx.agent.domain.task.state import (
    GoalResolution,
    GoalSpec,
    IntentResolution,
    PlanResolution,
    TaskState,
    goal_type_from_join,
)
from voidx.agent.domain.automation.workflow_dag import DEFAULT_WORKFLOW_DAG
from voidx.config import RetryConfig
from voidx.llm.structured import ainvoke_structured, resolve_structured_output_method
from voidx.llm.usage import (
    TokenUsage,
    UsageStats,
    estimate_context_tokens,
    estimate_message_tokens,
    extract_token_usage,
)
from voidx.platform.retry import retry_async


GOAL_RESOLVER_TIMEOUT_SECONDS = 20
class WorkflowName(str, Enum):
    BRAINSTORM = "brainstorm"
    DEBUG = "debug"
    DESIGN = "design"
    FEEDBACK = "feedback"
    PLAN = "plan"
    REVIEW = "review"
    TDD = "tdd"
    VERIFY = "verify"


class ResolverGoal(BaseModel):
    intent: Literal["coding", "general"] = "general"
    goal: str = Field(description="Stable overall objective for the current task. Keep it short, sharp, and clear.")
    workflow: WorkflowName | None = None
    kind_hint: str | None = None

    @model_validator(mode="after")
    def _goal_required_and_workflow_needs_goal(self) -> "ResolverGoal":
        if not self.goal or not self.goal.strip():
            raise ValueError("goal must be a non-empty string")
        if self.workflow is None:
            return self
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
        plan=PlanResolution(join=WorkflowName.BRAINSTORM, leave=WorkflowName.BRAINSTORM),
    )


def build_goal_resolution(user_text: str, task_state: TaskState) -> GoalResolution:
    """GOAL mode: construct result directly without LLM call.

    The user must specify a goal to enter goal mode. Fixed entry from plan node;
    plan will clarify via questions before planning.
    """
    goal = task_state.current_goal or GoalSpec(desc=user_text)
    return GoalResolution(
        intent=IntentResolution(type=TaskIntent.CODING),
        goal=goal,
        plan=PlanResolution(join=WorkflowName.PLAN, leave=None),
    )


async def resolve_goal_for_turn(
    *,
    model: Any | None,
    user_text: str,
    interaction_mode: str | InteractionMode | None,
    task_state: TaskState,
    log_diagnostic: bool = True,
    retry_config: RetryConfig | None = None,
    usage_stats: UsageStats | None = None,
    model_config: Any | None = None,
    resolver_model_factory: Any | None = None,
) -> GoalResolution:
    del interaction_mode
    fallback = GoalResolution(
        intent=IntentResolution(type=TaskIntent.GENERAL),
        goal=task_state.current_goal,
        plan=None,
    )
    fallback_reason = ""
    fallback_error_type = ""
    fallback_error = ""
    if model is None:
        fallback_reason = "model_unavailable"
        normalized = _normalize_resolution(fallback, user_text, task_state, is_fallback=True)
        _log_goal_resolver_decision(normalized, user_text, task_state, fallback_reason, fallback_error_type, fallback_error, enabled=log_diagnostic)
        return normalized

    if model_config is not None and resolver_model_factory is None:
        raise RuntimeError("resolver model factory is required with model_config")

    resolver_model = (
        resolver_model_factory(model, model_config)
        if resolver_model_factory is not None and model_config is not None
        else model
    )
    structured = getattr(resolver_model, "with_structured_output", None)
    if not callable(structured):
        fallback_reason = "structured_output_unsupported"
        normalized = _normalize_resolution(fallback, user_text, task_state, is_fallback=True)
        _log_goal_resolver_decision(normalized, user_text, task_state, fallback_reason, fallback_error_type, fallback_error, enabled=log_diagnostic)
        return normalized

    resolver_goal: ResolverGoal | None
    try:
        method = resolve_structured_output_method(resolver_model)
        resolver_messages = _resolver_messages_from_exchanges(
            user_text,
            task_state,
            json_mode=(method == "json_mode"),
        )
        rc = retry_config or RetryConfig()

        async def _invoke_once():
            return await ainvoke_structured(
                model=resolver_model,
                schema=ResolverGoal,
                messages=resolver_messages,
                method=method,
                include_raw=True,
                timeout=GOAL_RESOLVER_TIMEOUT_SECONDS,
            )

        raw = await retry_async(
            _invoke_once,
            max_attempts=rc.max_attempts,
            base_delay=rc.base_delay,
            max_delay=rc.max_delay,
            jitter=rc.jitter,
            label="goal_resolver",
            retry_on=(asyncio.TimeoutError, TimeoutError, ConnectionError, OSError),
        )
        if usage_stats is not None:
            _record_resolver_usage(
                usage_stats,
                model=resolver_model,
                messages=resolver_messages,
                response=raw,
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
        normalized = _normalize_resolution(fallback, user_text, task_state, is_fallback=True)
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




def _record_resolver_usage(
    usage_stats: UsageStats,
    *,
    model: Any,
    messages: list[BaseMessage],
    response: object,
) -> None:
    raw_message = _resolver_raw_message(response)
    model_name = _resolver_model_name(model)
    usage_stats.record_call(
        extract_token_usage(raw_message) if raw_message is not None else TokenUsage(),
        fallback_input_tokens=estimate_context_tokens(messages, model_name),
        fallback_output_tokens=estimate_message_tokens(
            raw_message if raw_message is not None else response,
            model_name,
        ),
        messages=messages,
        model=model_name,
        cache_key=f"goal_resolver:{model_name or type(model).__name__}",
    )


def _resolver_raw_message(response: object) -> AIMessage | None:
    if isinstance(response, AIMessage):
        return response
    if isinstance(response, dict):
        raw = response.get("raw")
        if isinstance(raw, AIMessage):
            return raw
    return None


def _resolver_model_name(model: Any) -> str:
    for attribute in ("model_name", "model"):
        value = getattr(model, attribute, None)
        if isinstance(value, str) and value:
            return value
    return ""


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
        active_workflows=active_workflow_names(task_state),
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
    if isinstance(value, dict):
        return {
            str(key): _raw_response_for_log(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_raw_response_for_log(item) for item in value]
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
        "- **goal**: Stable overall objective for the current task. Keep it short, sharp, and clear. Verb-first is preferred. Preserve material constraints while omitting transient execution detail. Never null or empty.\n"
        "- **workflow**: The workflow to enter, or null. Goal is always required regardless.\n"
        "- **kind_hint**: Optional semantic hint. Advisory only; never overrides workflow selection.\n"
        "\n"
        "## Workflow Selection Rules\n"
        "\n"
        "- workflow is null by default.\n"
        "- Only set workflow when this turn must enter a workflow gate before continuing.\n"
        "- Do not set workflow for read-only inspection, answering questions, explaining logs/code, or ordinary follow-up requests.\n"
        "- If an active workflow already covers the request, keep workflow null unless the user explicitly asks to start or switch workflows.\n"
        "\n"
        "## Available Workflows\n"
        "\n"
        "- brainstorm: Set only when the user wants to create or change behavior and requirements/design must be confirmed before implementation.\n"
        "- debug: Set only for actual bugs, crashes, failing tests, tracebacks, or unexpected behavior requiring root-cause investigation.\n"
        "- design: Set only when the user asks for a structured design doc, RFC, PRD, API doc, README, or changelog.\n"
        "- feedback: Set only when the user provides review feedback or requested optimizations to verify and implement.\n"
        "- plan: Set only when the user asks for an executable implementation plan before coding.\n"
        "- review: Set only when the user asks to review code, design, implementation, or changes.\n"
        "- tdd: Set only when the user asks to implement a clear coding change.\n"
        "- verify: Set only when the user asks to prove something is passing, fixed, complete, or safe.\n"
    )
    if json_mode:
        prompt += "\nRespond in JSON."
    return prompt


def _resolver_request_markdown(user_text: str, task_state: TaskState) -> str:
    recent_content = _recent_exchanges_content(task_state)
    active = ", ".join(active_workflow_names(task_state)) or "none"
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


_ALLOWED_JOIN_NODES = {WorkflowName.DEBUG, WorkflowName.BRAINSTORM, WorkflowName.DESIGN, WorkflowName.PLAN, WorkflowName.TDD, WorkflowName.REVIEW, WorkflowName.FEEDBACK, WorkflowName.VERIFY}


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
    goal = GoalSpec(desc=resolver.goal)
    if resolver.workflow is None:
        return GoalResolution(intent=IntentResolution(type=intent_type), goal=goal, plan=None)
    return GoalResolution(
        intent=IntentResolution(type=intent_type),
        goal=goal,
        plan=PlanResolution(join=resolver.workflow, leave=None),
    )


def _resolver_goal_from_goal_resolution(resolution: GoalResolution) -> ResolverGoal | None:
    goal = resolution.goal
    if goal is None:
        return None
    plan = resolution.plan
    return ResolverGoal(
        intent=resolution.intent.type.value,
        goal=goal.desc,
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
    *,
    is_fallback: bool = False,
) -> GoalResolution:
    if _is_short_continuation(user_text) and _has_completed_todos_without_remaining_work(task_state):
        return GoalResolution(
            intent=IntentResolution(type=TaskIntent.GENERAL),
            goal=task_state.current_goal,
            plan=None,
        )

    plan = resolution.plan
    if plan is not None:
        if plan.join and plan.join not in _ALLOWED_JOIN_NODES:
            plan = None
        elif plan.leave and plan.leave not in DEFAULT_WORKFLOW_DAG.nodes:
            plan = PlanResolution(join=plan.join, leave=None)

    if resolution.intent.type == TaskIntent.GENERAL:
        current_join = _current_active_join(task_state)
        if current_join and task_state.current_goal is not None and is_fallback:
            return GoalResolution(
                intent=IntentResolution(type=TaskIntent.CODING),
                goal=task_state.current_goal,
                plan=PlanResolution(join=current_join, leave=None),
            )
        goal = resolution.goal or task_state.current_goal
        return GoalResolution(
            intent=resolution.intent,
            goal=goal,
            plan=None,
        )

    goal = resolution.goal or task_state.current_goal
    if plan is None or not plan.join:
        current_join = _current_active_join(task_state)
        if current_join and task_state.current_goal is not None and is_fallback and _is_short_continuation(user_text):
            return GoalResolution(
                intent=resolution.intent,
                goal=task_state.current_goal,
                plan=PlanResolution(join=current_join, leave=None),
            )
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


def _has_completed_todos_without_remaining_work(task_state: TaskState) -> bool:
    todo_state = task_state.todo_state
    if todo_state is None or todo_state.total <= 0:
        return False
    return todo_state.active <= 0 and todo_state.pending <= 0 and todo_state.done >= todo_state.total


