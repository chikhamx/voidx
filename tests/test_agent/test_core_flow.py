"""Regression tests for core graph behavior."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from voidx.agent.graph import VoidXGraph
from voidx.config import Config
from voidx.memory.session import (
    MessageRow,
    create_session,
    delete_session,
    load_messages,
    save_message,
)
from voidx.permission.service import PermissionService
from voidx.ui.dock import BottomInputDock, set_dock


def _graph(tmp_path):
    cfg = Config(workspace=str(tmp_path))
    return VoidXGraph(cfg, api_key=None)


def test_permission_decision_keeps_task_in_ask_bucket():
    service = PermissionService()

    assert service.decide("task", "implement") == "ask"


@pytest.mark.asyncio
async def test_graph_authorization_does_not_auto_allow_task(tmp_path):
    graph = _graph(tmp_path)

    async def deny(_tool_calls):
        return "n"

    graph._ask_tool_permission = deny
    approved, denied = await graph._authorize_tool_calls(
        [{"name": "task", "args": {"subagent_type": "implement"}, "id": "call_1"}],
        agent_name="orchestrator",
        plan_mode=False,
        session_id="test",
    )

    assert approved == []
    assert len(denied) == 1
    assert "User denied" in denied[0][1]


@pytest.mark.asyncio
async def test_graph_authorization_respects_session_deny_for_safe_bash(tmp_path):
    graph = _graph(tmp_path)
    graph._permission.deny_silent("bash")

    approved, denied = await graph._authorize_tool_calls(
        [{"name": "bash", "args": {"command": "ls"}, "id": "call_1"}],
        agent_name="orchestrator",
        plan_mode=False,
        session_id="test",
    )

    assert approved == []
    assert len(denied) == 1
    assert "Permission denied" in denied[0][1]


@pytest.mark.asyncio
async def test_session_persistence_saves_only_new_ai_and_tool_messages(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        await save_message(MessageRow(session_id=session.id, role="user", content="old question"))
        await save_message(MessageRow(session_id=session.id, role="assistant", content="old answer"))

        graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                return {"messages": list(initial["messages"]) + [AIMessage(content="new answer")]}

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph._run_once("new question")
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        rows = await load_messages(session.id)
        assistant_contents = [row.content for row in rows if row.role == "assistant"]
        assert assistant_contents.count("old answer") == 1
        assert assistant_contents.count("new answer") == 1
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_run_once_persists_image_attachment_as_structured_user_message(tmp_path):
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = VoidXGraph(Config(workspace=str(tmp_path)), api_key=None, session=session)

        class FakeGraph:
            async def ainvoke(self, initial, _config):
                user = initial["messages"][-1]
                assert isinstance(user.content, list)
                assert user.content[1]["type"] == "image_url"
                return {"messages": list(initial["messages"]) + [AIMessage(content="ok")]}

        graph.graph = FakeGraph()

        test_dock = BottomInputDock()
        set_dock(test_dock)
        test_dock.begin_capture()
        try:
            await graph._run_once("describe @shot.png")
        finally:
            test_dock.deactivate()
            test_dock.reset()
            set_dock(None)

        rows = await load_messages(session.id)
        user_rows = [row for row in rows if row.role == "user"]
        assert user_rows[-1].content_format == "structured"
        assert "image_url" in user_rows[-1].content
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_compaction_trims_head_and_injects_summary_into_system_prompt(tmp_path):
    graph = _graph(tmp_path)
    graph._compaction.is_overflow = lambda _tokens: True
    graph._compaction.select = lambda messages: (messages[:-1], "tail")

    async def summarize(_head_messages, _previous_summary):
        return "summary text"

    graph._run_compaction_agent = summarize
    messages = [
        HumanMessage(content="old question"),
        AIMessage(content="old answer"),
        HumanMessage(content="current question", id="current_user"),
    ]

    await graph._maybe_compact(messages, [])

    assert len(messages) == 1
    assert isinstance(messages[0], HumanMessage)
    assert graph._pending_summary == "summary text"

    state = {
        "messages": messages,
        "workspace": str(tmp_path),
        "agent": "orchestrator",
        "plan_mode": False,
        "tool_results": {},
        "step_count": 0,
        "max_steps": 50,
        "should_continue": True,
    }

    await graph._prepare_with_stream(state)

    assert isinstance(messages[0], SystemMessage)
    assert "Conversation Summary" in messages[0].content
    assert "summary text" in messages[0].content
    assert "You are voidx" in messages[0].content
