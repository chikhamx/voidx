"""End-to-end chat profile tests over the real LangGraph execution chain."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

import voidx.memory.store as store

from voidx.agent.application.chat_service import ChatService
from voidx.agent.infrastructure.langgraph.adapter import LangGraphTurnEngine
from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from voidx.agent.infrastructure.memory_session import MemorySessionAdapter
from voidx.agent.infrastructure.null_events import NullEventPublisher
from voidx.agent.runtime import AgentRuntime
from voidx.config import Config
from voidx.memory.session import create_session, delete_session, get_session, load_messages
from voidx.ui.output.dock import BottomInputDock, set_dock


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
