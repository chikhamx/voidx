"""Compose the existing LangGraph runtime without presentation capabilities."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4

from voidx.agent.adapters.langgraph.execution import LangGraphExecution
from voidx.agent.adapters.langgraph.runtime.semantic_output import SemanticOutput
from voidx.agent.adapters.persistence.session_repository import create_session, get_session
from voidx.agent.application.runtime.interaction_coordinator import InteractionCoordinator
from voidx.agent.application.runtime.run_supervisor import RunResult, RunSupervisor
from voidx.agent.application.runtime.semantic_channel import SemanticChannel
from voidx.agent.domain import semantic_events as e
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.adapters.persistence.headless_locks import (
    HeadlessFileLock, HeadlessWorkspaceWriteLock, normalize_workspace,
)
from voidx.bootstrap.autonomous_headless import _resolve_session_profile
from voidx.bootstrap.permission import build_permission_service
from voidx.bootstrap.providers import build_model_catalog
from voidx.bootstrap.skills import build_skills_api_provider
from voidx.bootstrap.tooling import bind_scoped_tools, build_tool_registry, scoped_tool_registry
from voidx.config import Config, Settings


@dataclass
class HeadlessInitializationIdentity:
    session_id: str


_initialization_identity: ContextVar[HeadlessInitializationIdentity | None] = ContextVar(
    "headless_initialization_identity", default=None,
)


@contextmanager
def headless_initialization(session_id: str) -> Iterator[HeadlessInitializationIdentity]:
    identity = HeadlessInitializationIdentity(session_id)
    token = _initialization_identity.set(identity)
    try:
        yield identity
    finally:
        _initialization_identity.reset(token)


class _RunPublisher:
    """TurnRunner persists before reporting completion; only the supervisor seals it."""

    def __init__(self, channel):
        self.channel = channel
        self.completed = None

    async def publish(self, event):
        if isinstance(event, e.TurnCompleted):
            self.completed = event.payload
        elif isinstance(event, (e.TurnFailed, e.TurnCancelled)):
            # TurnRunner re-raises these outcomes to the supervisor.
            return
        else:
            await self.channel.publish(event)


async def build_headless_run(config: Config, *, settings: Settings | None,
                             prompt: str, session_id: str, workspace: str):
    workspace = normalize_workspace(workspace)
    config = config.model_copy(update={"workspace": workspace}, deep=True)
    settings = settings or Settings(workspace)
    api_key = await settings.resolve_api_key(config.model.provider)
    if not api_key:
        raise ValueError("No API key configured for the selected provider")
    session = await get_session(session_id) if session_id else await create_session(
        workspace=workspace, provider=config.model.provider, model=config.model.model,
    )
    if session is None:
        raise ValueError("Unknown session_id")
    if normalize_workspace(session.workspace) != workspace:
        raise ValueError("session_id workspace does not match requested workspace")
    identity = _initialization_identity.get()
    if identity is not None:
        identity.session_id = session.id
    session_lock = HeadlessFileLock("session", session.id)
    await session_lock.acquire()
    try:
        session = await get_session(session.id)
        if session is None:
            raise ValueError("Unknown session_id")
        if normalize_workspace(session.workspace) != workspace:
            raise ValueError("session_id workspace does not match requested workspace")
        if _resolve_session_profile(session, workspace).runtime_profile.profile_id in {"loop", "goal"}:
            from voidx.bootstrap.autonomous_headless import build_sdk_loop_run
            return await build_sdk_loop_run(
                config, settings, api_key, prompt, session, workspace, session_lock,
                execution_factory=_build_execution, publisher_factory=_RunPublisher,
            )
        return await _build_session_run(config, settings, api_key, prompt, session, workspace, session_lock)
    except BaseException:
        session_lock.release()
        raise


async def _build_session_run(config, settings, api_key, prompt, session, workspace, session_lock):
    identity = dict(session_id=session.id, thread_id=session.id, turn_id=str(uuid4()))
    channel = SemanticChannel(**identity)
    publisher = _RunPublisher(channel)
    output = SemanticOutput(publisher, **identity)
    interactions = InteractionCoordinator(channel, **identity)
    workspace_lock = HeadlessWorkspaceWriteLock(workspace)
    execution, context = await _build_execution(
        config, settings, api_key, session, workspace, output, interactions, workspace_lock,
    )
    from voidx.bootstrap.autonomous_headless import build_autonomous_session
    assembly = build_autonomous_session(execution, workspace=workspace, session_id=session.id)

    from voidx.agent.application.runtime.contracts import TurnRequest
    from voidx.agent.domain.thread import AgentThread

    from voidx.bootstrap.managed_guidance import current_guidance
    managed_guidance = current_guidance.get()
    if managed_guidance is not None:
        from voidx.agent.application.guidance_service import GuidanceService
        from voidx.agent.adapters.persistence.thread_repository import ThreadStore
        guidance = GuidanceService(ThreadStore())
        execution.bind_guidance_service(guidance)
        managed_guidance.activate(guidance, context)

    async def execute(supervisor):
        await assembly.runtime.run_turn(TurnRequest(
            thread=AgentThread(thread_id=context.thread_id, session_id=session.id,
                               workspace=workspace),
            user_text=prompt, context=context,
        ))
        if publisher.completed is None:
            raise RuntimeError("TurnRunner returned without completion")
        return RunResult(completed=publisher.completed)

    async def persist(result):
        await execution.persist_runtime_state()

    async def cleanup(outcome):
        try:
            return await interactions.cleanup(outcome)
        finally:
            try:
                await execution.aclose()
            finally:
                workspace_lock.release()
                session_lock.release()

    run = RunSupervisor(channel, execute=execute, persist=persist,
                        cleanup=cleanup, before_cancel=interactions.begin_cancel)
    return run, interactions


async def _build_execution(config, settings, api_key, session, workspace, output, interactions, workspace_lock):
    from voidx.llm.adapters.langchain_model_factory import create_chat_model, create_resolver_model
    from voidx.llm.domain.model import ReasoningEffort
    from voidx.llm.domain.provider import get_context_limit
    from voidx.llm.providers.catalog import PROVIDER_SPECS
    from voidx.agent.application.prompts import language_labels
    from voidx.agent.application.runtime_context import tone_labels

    skills = build_skills_api_provider(workspace, settings)

    def notify_permission(message):
        # The legacy notifier is synchronous: do not create detached publishers.
        raise RuntimeError(f"Unexpected synchronous permission notification: {message}")

    execution = LangGraphExecution.__new__(LangGraphExecution)
    try:
        LangGraphExecution.__init__(execution,
            config, api_key, session=session, settings=settings, ui=None,
            workspace_write_lock=workspace_lock, semantic_output=output,
            interaction_requester=interactions.request,
            permission_notifier=notify_permission, permission_service_factory=build_permission_service,
            model_factory=create_chat_model, resolver_model_factory=create_resolver_model,
            model_catalog=build_model_catalog(settings), model_catalog_factory=build_model_catalog,
            skills_api=skills(workspace), skills_api_provider=skills,
            skills_api_factory=lambda value: skills.replace(workspace, value),
            tool_registry_factory=build_tool_registry, scoped_tools_binder=bind_scoped_tools,
            profile_tool_registry_factory=scoped_tool_registry, slash_handler_factory=lambda host: None,
            reasoning_effort_type=ReasoningEffort, context_limit_resolver=get_context_limit,
            provider_specs=PROVIDER_SPECS, language_labels=language_labels(), tone_labels=tone_labels(),
        )
        from voidx.agent.application.profile_tool_policy import default_profile_tool_policy_for

        resolved = _resolve_session_profile(session, workspace)
        context = TurnExecutionContext(
            session_id=session.id, thread_id=session.id,
            runtime_profile=resolved.runtime_profile,
            workflow_context=resolved.workflow_context, workspace=workspace,
            tool_policy=default_profile_tool_policy_for(resolved),
        )
        await execution.restore_runtime_state()
    except BaseException as error:
        try:
            await execution.aclose()
        except BaseException as cleanup_error:
            raise BaseExceptionGroup("Headless initialization and cleanup failed", [error, cleanup_error])
        raise

    return execution, context


