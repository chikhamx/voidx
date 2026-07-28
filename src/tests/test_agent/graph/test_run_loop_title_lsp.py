"""Tests for run loop title generation, LSP, compaction, and misc."""

import asyncio
import contextlib
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
import voidx.memory.store as store


from voidx.agent.slash import SlashHandler
from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from voidx.agent.application.agent_service import AgentService
from voidx.agent.infrastructure.langgraph.execution import _sanitize_generated_title
from voidx.agent.application.runtime_context import InteractionMode, TaskIntent
from voidx.runtime.task_state import (
    GoalResolution,
    GoalSpec,
    IntentResolution,
    PlanResolution,
    TaskState,
)
from voidx.config import Config
from voidx.llm.usage import UsageStats
from voidx.memory.runtime_state import RuntimeStateSnapshot, save_runtime_state
from voidx.memory.session import MessageRow, create_session, get_session, load_messages, save_message, update_title
from voidx.selfupdate import UpdateCheckResult
from voidx.workflow.runtime import WorkflowActivationSource, WorkflowRunState, WorkflowRunStatus
from voidx.tools.task_tracker import TaskTracker
from voidx.ui.output.dock import BottomInputDock, set_dock
from voidx.ui.output.events import DockEventConsumer, ui_events
from voidx.ui.protocol import UiSubmitCommand
from voidx.runtime.ui_port import runtime_ui_port
from tests.test_agent.graph.run_loop_helpers import (
    FakeTui,
    ExitTui,
    NoopMcpManager,
    NoopLspManager,
    _graph,
    _service,
    _disable_external_managers,
)


@pytest.mark.asyncio
async def test_smart_title_requires_database_title_to_remain_temporary(tmp_path):
    session = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    await update_title(session.id, "temporary")
    session = session.model_copy(update={"title": "temporary"})
    graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None, session=session)
    graph._schedule_session_title_generation(session.id, "first request", "temporary")
    assert graph._title_task is None

    await update_title(session.id, "Manual title")

    loaded = await get_session(session.id)
    assert loaded is not None
    assert loaded.title == "Manual title"


@pytest.mark.asyncio
async def test_title_auto_uses_first_user_message(tmp_path):
    session = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    await save_message(MessageRow(session_id=session.id, role="user", content="first user request"))
    await save_message(MessageRow(session_id=session.id, role="assistant", content="response"))
    await save_message(MessageRow(session_id=session.id, role="user", content="second user request"))
    graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None, session=session)
    prompts: list[str] = []

    class FakeTitleModel:
        async def ainvoke(self, messages):
            prompts.append(messages[1].content)
            return AIMessage(content="First request title")

    graph.model = FakeTitleModel()

    assert await graph.regenerate_session_title() is True
    assert graph._title_task is None

    loaded = await get_session(session.id)
    assert loaded is not None
    assert loaded.title == "first user request"
    assert prompts == []


def test_sanitize_generated_title_rejects_markdown():
    assert _sanitize_generated_title("**Bold title**") == ""
    assert _sanitize_generated_title("# Heading title") == ""
    assert _sanitize_generated_title("`code title`") == ""
    assert _sanitize_generated_title("[Title](https://example.com)") == ""
    assert _sanitize_generated_title("Fix login-flow bug") == "Fix login-flow bug"


@pytest.mark.asyncio
async def test_delete_empty_current_session_only_deletes_sessions_without_messages(tmp_path):
    empty = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None, session=empty)

    await graph.delete_empty_current_session()

    assert await get_session(empty.id) is None
    assert graph._session is None

    non_empty = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    await save_message(MessageRow(session_id=non_empty.id, role="user", content="hello"))
    graph = LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None, session=non_empty)

    await graph.delete_empty_current_session()

    assert await get_session(non_empty.id) is not None
    assert graph._session is not None


@pytest.mark.asyncio
async def test_exit_cleanup_deletes_empty_current_session(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx.agent.application.agent_service.create_frontend", ExitTui)
    session = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    execution = LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None, session=session)
    graph = _service(execution)
    _disable_external_managers(graph)
    test_dock = BottomInputDock()
    set_dock(test_dock)
    try:
        await graph.run()
    finally:
        test_dock.reset()
        set_dock(None)

    assert await get_session(session.id) is None
    assert execution._session is None


@pytest.mark.asyncio
async def test_exit_cleanup_keeps_session_with_messages_even_new_session_title(tmp_path, monkeypatch):
    monkeypatch.setattr("voidx.agent.application.agent_service.create_frontend", ExitTui)
    session = await create_session(workspace=str(tmp_path), provider="mimo", model="mimo-v2.5")
    await save_message(MessageRow(session_id=session.id, role="user", content="hello"))
    await update_title(session.id, "New session")
    execution = LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None, session=session)
    graph = _service(execution)
    _disable_external_managers(graph)
    test_dock = BottomInputDock()
    set_dock(test_dock)
    try:
        await graph.run()
    finally:
        test_dock.reset()
        set_dock(None)

    loaded = await get_session(session.id)
    assert loaded is not None
    assert loaded.title == "New session"
    assert loaded.message_count == 1
    assert execution._session is not None
    assert execution._session.id == session.id


