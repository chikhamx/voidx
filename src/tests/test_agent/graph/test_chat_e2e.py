"""End-to-end chat profile tests over the real LangGraph execution chain."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

import voidx.memory.store as store

from voidx.agent.application.chat_service import ChatService
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.prompts import BaseSystemProfile
from voidx.agent.infrastructure.langgraph.adapter import LangGraphTurnEngine
from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from voidx.agent.infrastructure.memory_session import MemorySessionAdapter
from voidx.agent.infrastructure.null_events import NullEventPublisher
from voidx.agent.runtime import AgentRuntime
from voidx.config import Config
from voidx.memory.session import create_session, delete_session, get_session, load_messages
from voidx.ui.output.dock import BottomInputDock, set_dock
from voidx.ui.output.types import ThreadExecutionContext


@pytest.fixture(autouse=True)
def isolated_memory_store(tmp_path):
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    store.DATA_DIR = tmp_path / ".voidx"
    yield
    if store._conn is not None:
        store._conn.close()
    store._conn = None


def _runtime(execution) -> AgentRuntime:
    return AgentRuntime(
        SimpleNamespace(
            turn_engine=LangGraphTurnEngine(execution),
            sessions=MemorySessionAdapter(),
            events=NullEventPublisher(),
        )
    )


@pytest.mark.asyncio
async def test_chat_turn_runs_in_isolated_session_with_tool_view(tmp_path):
    coding = await create_session(workspace=str(tmp_path), profile="coding")
    try:
        execution = LangGraphExecution(
            Config(workspace=str(tmp_path)),
            api_key=None,
            session=coding,
        )

        captured = {}

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                captured["active_view"] = getattr(execution, "_active_chat_tool_view", None)
                captured["session_id"] = execution._session.id
                return {"messages": list(initial["messages"]) + [AIMessage(content="chat answer")]}

        execution.graph = FakeGraph()

        service = ChatService(_runtime(execution))
        dock = BottomInputDock()
        set_dock(dock)
        dock.begin_capture()
        try:
            result = await service.run_turn(user_text="hello chat", workspace=tmp_path)
        finally:
            dock.deactivate()
            dock.reset()
            set_dock(None)

        # Lazy identity resolved and returned through TurnResult.
        assert result.session_id is not None
        assert result.thread.thread_id == f"chat:{result.session_id}"
        chat_session = await get_session(result.session_id)
        assert chat_session is not None
        assert chat_session.runtime_profile == "chat"

        # The turn ran bound to the chat session with an active chat tool view.
        assert captured["session_id"] == result.session_id
        assert captured["active_view"] is not None
        assert captured["active_view"].allows("read") is True
        assert captured["active_view"].allows("bash") is False

        # Transcript landed in the chat session, not the coding host session.
        chat_rows = await load_messages(result.session_id)
        assert any(getattr(row, "role", "") == "user" for row in chat_rows)
        coding_rows = await load_messages(coding.id)
        assert all(getattr(row, "role", "") != "user" for row in coding_rows)

        # Host coding session identity is untouched by the borrowed chat turn.
        assert execution._session.id == coding.id
    finally:
        await delete_session(coding.id)


@pytest.mark.asyncio
async def test_chat_resumed_thread_keeps_own_session(tmp_path):
    coding = await create_session(workspace=str(tmp_path), profile="coding")
    try:
        execution = LangGraphExecution(
            Config(workspace=str(tmp_path)),
            api_key=None,
            session=coding,
        )

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

        execution.graph = FakeGraph()
        service = ChatService(_runtime(execution))

        dock = BottomInputDock()
        set_dock(dock)
        dock.begin_capture()
        try:
            first = await service.run_turn(user_text="first", workspace=tmp_path)
            second = await service.run_turn(thread=first.thread, user_text="second", workspace=tmp_path)
        finally:
            dock.deactivate()
            dock.reset()
            set_dock(None)


        assert second.session_id == first.session_id
        assert second.thread.thread_id == first.thread.thread_id
        rows = await load_messages(first.session_id)
        user_texts = [getattr(row, "content", "") for row in rows if getattr(row, "role", "") == "user"]
        assert any("first" in text for text in user_texts)
        assert any("second" in text for text in user_texts)
    finally:
        await delete_session(coding.id)


def _system_text(messages):
    for msg in messages:
        cls = msg.__class__.__name__
        if getattr(msg, "type", "") == "system" or cls == "SystemMessage":
            return str(getattr(msg, "content", ""))
    # Fallback: first message if it looks like a system prompt
    if messages:
        return str(getattr(messages[0], "content", ""))
    return ""


@pytest.mark.asyncio
async def test_chat_system_prompt_excludes_coding_persona_and_workflow(tmp_path):
    coding = await create_session(workspace=str(tmp_path), profile="coding")
    try:
        execution = LangGraphExecution(
            Config(workspace=str(tmp_path)),
            api_key=None,
            session=coding,
        )

        captured = {}

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                await execution._prepare_with_stream(initial)
                captured["system"] = _system_text(initial["messages"])
                return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

        execution.graph = FakeGraph()
        service = ChatService(_runtime(execution))
        service = ChatService(_runtime(execution))
        dock = BottomInputDock()
        set_dock(dock)
        dock.begin_capture()
        try:
            await service.run_turn(user_text="hello", workspace=tmp_path)
        finally:
            dock.deactivate()
            dock.reset()
            set_dock(None)

        system = captured["system"]
        assert "## Base System\nYou are voidx, a conversational assistant." in system
        assert "Profile Directive" in system
        assert "chat" in system.lower()
        assert "Persona Model" not in system
        assert "Workflow Runtime" not in system
        assert "Current Task State" not in system
        assert "### Runtime Rules" not in system
        assert "### Workspace Rules" not in system
        assert "### Delegation Rules" not in system
        assert "Show progress via todo" not in system
        assert "### Verification Rules" in system
        assert "### Collaboration Rules" in system
        assert "Summarize outcomes" in system
        assert "coding assistant" not in system
    finally:
        await delete_session(coding.id)


@pytest.mark.asyncio
async def test_custom_profile_policy_selects_base_system_without_profile_branch(tmp_path):
    coding = await create_session(workspace=str(tmp_path), profile="coding")
    try:
        execution = LangGraphExecution(
            Config(workspace=str(tmp_path)),
            api_key="test-key",
            session=coding,
        )

        captured = {}

        class CustomPromptPolicy:
            @property
            def base_system_spec(self):
                return BaseSystemProfile(
                    identity="You are voidx, a research assistant.",
                    style_names=["language", "summarize_results"],
                    global_section_names={
                        "Verification Rules": ["fresh_verification"],
                    },
                )

            @property
            def persona_prompt(self):
                return ""

            @property
            def workflow_runtime(self):
                return ""

            @property
            def task_state_section(self):
                return ""

            @property
            def profile_directive(self):
                return "Custom profile directive."

        profile = RuntimeProfile(
            profile_id="research",
            revision=1,
            name="Research",
            prompt_policy=CustomPromptPolicy(),
        )
        context = ThreadExecutionContext(
            thread_id=coding.id,
            session_id=coding.id,
            profile=profile,
        )

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                await execution._prepare_with_stream(initial)
                captured["system"] = _system_text(initial["messages"])
                return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

        execution.graph = FakeGraph()
        dock = BottomInputDock()
        set_dock(dock)
        dock.begin_capture()
        try:
            await execution.run_turn("hello", context=context)
        finally:
            dock.deactivate()
            dock.reset()
            set_dock(None)

        system = captured["system"]
        assert "## Base System\nYou are voidx, a research assistant." in system
        assert "Custom profile directive." in system
        assert "Persona Model" not in system
        assert "Workflow Runtime" not in system
        assert "Current Task State" not in system
        assert "### Verification Rules" in system
        assert "### Workspace Rules" not in system
    finally:
        await delete_session(coding.id)


@pytest.mark.asyncio
async def test_coding_system_prompt_still_includes_persona_and_workflow(tmp_path):
    coding = await create_session(workspace=str(tmp_path), profile="coding")
    try:
        execution = LangGraphExecution(
            Config(workspace=str(tmp_path)),
            api_key=None,
            session=coding,
        )

        captured = {}

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                await execution._prepare_with_stream(initial)
                captured["system"] = _system_text(initial["messages"])
                return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

        execution.graph = FakeGraph()
        dock = BottomInputDock()
        set_dock(dock)
        dock.begin_capture()
        try:
            await execution.run_turn("hello")
        finally:
            dock.deactivate()
            dock.reset()
            set_dock(None)

        system = captured["system"]
        assert "Persona Model" in system
        assert "Workflow Runtime" in system
        assert "Current Task State" in system
        assert "Profile Directive" not in system
    finally:
        await delete_session(coding.id)
