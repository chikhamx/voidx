"""Explicit LangGraph execution composition for tests."""

from __future__ import annotations

from voidx.agent.adapters.langgraph.execution import LangGraphExecution
from tests.presentation_ui import make_presentation_ui
from voidx.agent.ports.workspace_lock import NullWorkspaceWriteLock


def make_langgraph_execution(*args, ui=None, **kwargs) -> LangGraphExecution:
    if "skills_api" not in kwargs:
        from voidx.skills.application.api import SkillsApi
        from voidx.skills.registry import SkillRegistry
        from voidx.skills.service import SkillService

        workspace = str(getattr(args[0], "workspace", ".")) if args else "."
        api = SkillsApi(SkillService(SkillRegistry(workspace)))
        kwargs["skills_api"] = api
        kwargs["skills_api_provider"] = lambda _workspace: api
    if "update_service" not in kwargs:
        from voidx.update import service as update_service

        kwargs["update_service"] = update_service
    if "clipboard_image" not in kwargs:
        from voidx.presentation.tools import clipboard_image

        kwargs["clipboard_image"] = clipboard_image
    if "model_factory" not in kwargs or "resolver_model_factory" not in kwargs:
        from voidx.llm.adapters.langchain_model_factory import (
            create_chat_model,
            create_resolver_model,
        )

        kwargs.setdefault("model_factory", create_chat_model)
        kwargs.setdefault("resolver_model_factory", create_resolver_model)
    if (
        "tool_registry_factory" not in kwargs
        or "scoped_tools_binder" not in kwargs
        or "profile_tool_registry_factory" not in kwargs
    ):
        from voidx.bootstrap.tooling import (
            bind_scoped_tools,
            build_tool_registry,
            scoped_tool_registry,
        )

        kwargs.setdefault("tool_registry_factory", build_tool_registry)
        kwargs.setdefault("scoped_tools_binder", bind_scoped_tools)
        kwargs.setdefault("profile_tool_registry_factory", scoped_tool_registry)
    if "slash_handler_factory" not in kwargs:
        from voidx.agent.adapters.persistence import session_cleanup
        from voidx.agent.adapters.persistence.session_adapter import SessionRepositoryAdapter
        from voidx.bootstrap.slash import build_slash_handler
        from voidx.presentation.slash import SlashHandler

        def slash_handler_factory(host):
            return build_slash_handler(
                host,
                session_repository=SessionRepositoryAdapter(),
                session_cleanup=session_cleanup,
            )

        kwargs["slash_handler_factory"] = slash_handler_factory
    if "reasoning_effort_type" not in kwargs or "context_limit_resolver" not in kwargs:
        from voidx.llm.domain.model import ReasoningEffort
        from voidx.llm.domain.provider import get_context_limit

        kwargs.setdefault("reasoning_effort_type", ReasoningEffort)
        kwargs.setdefault("context_limit_resolver", get_context_limit)
    if "provider_specs" not in kwargs:
        from voidx.llm.providers.catalog import PROVIDER_SPECS

        kwargs["provider_specs"] = PROVIDER_SPECS
    if "language_labels" not in kwargs or "tone_labels" not in kwargs:
        from voidx.agent.application.prompts import language_labels
        from voidx.agent.application.runtime_context import tone_labels

        kwargs.setdefault("language_labels", language_labels())
        kwargs.setdefault("tone_labels", tone_labels())
    if "permission_service_factory" not in kwargs:
        from voidx.tooling.adapters.permission.in_memory_state import create_permission_service

        def permission_service_factory(config, *, settings=None, notifier):
            return create_permission_service(
                permission_mode=config.permission_mode.value,
                sandbox_readable_files=list(config.sandbox_readable_files),
                sandbox_readable_dirs=list(config.sandbox_readable_dirs),
                sandbox_writable_files=list(config.sandbox_writable_files),
                sandbox_writable_dirs=list(config.sandbox_writable_dirs),
                notifier=notifier,
            )

        kwargs["permission_service_factory"] = permission_service_factory

    return LangGraphExecution(
        *args,
        ui=ui or make_presentation_ui(),
        workspace_write_lock=kwargs.pop("workspace_write_lock", NullWorkspaceWriteLock()),
        **kwargs,
    )
