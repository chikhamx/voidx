from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from voidx.agent.domain.task.intent import InteractionMode
from voidx.agent.domain.task.state import TaskState
from voidx.update import service as update_service
from voidx.presentation.tools import clipboard_image
from voidx.llm.domain.model import ReasoningEffort
from voidx.llm.domain.provider import get_context_limit
from voidx.llm.providers.catalog import PROVIDER_SPECS
from voidx.agent.application.prompts import _LANGUAGE_LABELS
from voidx.agent.application.runtime_context import _TONE_LABELS


def command_context(**overrides: Any) -> SimpleNamespace:
    async def noop_async(*_args: Any, **_kwargs: Any) -> bool:
        return False

    context = SimpleNamespace(
        app=None,
        settings=None,
        session=None,
        permission=None,
        task_state=TaskState(),
        workspace=".",
        mcp_manager=None,
        loop_service=None,
        goal_service=None,
        _interaction_mode=None,
        _plan_mode=False,
    )

    def set_interaction_mode(mode: InteractionMode) -> InteractionMode:
        context._interaction_mode = mode
        context._plan_mode = mode is InteractionMode.PLAN
        return mode

    def set_task_state(state: TaskState) -> None:
        context.task_state = state

    def clear_successful_dangerous_calls() -> None:
        context._successful_dangerous_calls = set()
        context._successful_dangerous_calls_session_id = None

    def model_factory(*args: Any, **kwargs: Any):
        from voidx.llm.adapters import langchain_model_factory

        return langchain_model_factory.create_chat_model(*args, **kwargs)

    defaults: dict[str, Any] = {
        "interaction_mode_value": lambda: (
            context._interaction_mode.value
            if context._interaction_mode is not None
            else InteractionMode.AUTO.value
        ),
        "set_interaction_mode": set_interaction_mode,
        "set_task_state": set_task_state,
        "clear_successful_dangerous_calls": clear_successful_dangerous_calls,
        "invalidate_skill_service_cache": lambda: None,
        "regenerate_session_title": noop_async,
        "persist_runtime_state": noop_async,
        "set_session_title": noop_async,
        "get_aiapproval_config": lambda: None,
        "update_service": update_service,
        "clipboard_image": clipboard_image,
        "_model_factory": model_factory,
        "reasoning_effort_type": ReasoningEffort,
        "context_limit_resolver": get_context_limit,
        "provider_specs": PROVIDER_SPECS,
        "language_labels": _LANGUAGE_LABELS,
        "tone_labels": _TONE_LABELS,
    }
    for name, value in defaults.items():
        setattr(context, name, value)
    for name, value in overrides.items():
        setattr(context, name, value)
    if not hasattr(context, "model_catalog"):
        from voidx.bootstrap.providers import build_model_catalog

        context.model_catalog = build_model_catalog(context.settings)
    if not hasattr(context, "can_submit_guidance"):
        context.can_submit_guidance = lambda: hasattr(context, "submit_guidance")
    return context
