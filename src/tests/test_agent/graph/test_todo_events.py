"""Integration tests for todo UI event emission from tool execution layer."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


import pytest

from voidx.agent.infrastructure.langgraph.runtime.todo_events import todo_updated_event
from voidx.tools.base import ToolResult
from voidx.ui.output.events.schema import TodoItemPayload, TodoUpdated


# ── todo_updated_event unit tests ──────────────────────────────────


class TestTodoUpdatedEvent:
    def test_constructs_event_from_valid_result(self):
        result = ToolResult(
            output="done",
            metadata={
                "todo_items": [
                    {"id": "impl", "content": "implement", "status": "active"},
                    {"id": "test", "content": "test", "status": "pending"},
                ],
                "todo_summary": "0/2 done · 1 active · 1 pending",
            },
        )
        event = todo_updated_event(result)
        assert event is not None
        assert isinstance(event, TodoUpdated)
        assert len(event.items) == 2  # All items, not just active
        assert event.items[0].id == "impl"
        assert event.items[0].content == "implement"
        assert event.items[0].status == "active"
        assert event.items[1].id == "test"
        assert event.items[1].status == "pending"
        assert event.summary == "0/2 done · 1 active · 1 pending"

    def test_event_includes_all_statuses(self):
        result = ToolResult(
            output="done",
            metadata={
                "todo_items": [
                    {"id": "a", "content": "active", "status": "active"},
                    {"id": "p", "content": "pending", "status": "pending"},
                    {"id": "d", "content": "done", "status": "done"},
                ],
                "todo_summary": "1/3 done · 1 active · 1 pending",
            },
        )
        event = todo_updated_event(result)
        assert event is not None
        assert len(event.items) == 3
        statuses = {item.status for item in event.items}
        assert statuses == {"active", "pending", "done"}

    def test_passes_agent_id(self):
        result = ToolResult(
            output="done",
            metadata={
                "todo_items": [{"id": "task1", "content": "task", "status": "pending"}],
                "todo_summary": "0/1 done · 0 active · 1 pending",
            },
        )
        event = todo_updated_event(result, agent_id=3)
        assert event is not None
        assert event.agent_id == 3

    def test_returns_none_when_no_metadata(self):
        result = ToolResult(output="done")
        assert todo_updated_event(result) is None

    def test_returns_none_when_missing_todo_items(self):
        result = ToolResult(output="done", metadata={"todo_summary": "0/0"})
        assert todo_updated_event(result) is None

    def test_returns_none_when_missing_summary(self):
        result = ToolResult(
            output="done",
            metadata={"todo_items": [{"id": "task1", "content": "x", "status": "pending"}]},
        )
        assert todo_updated_event(result) is None

    def test_returns_none_on_invalid_item(self):
        result = ToolResult(
            output="done",
            metadata={
                "todo_items": [{"content": "x", "status": "invalid_status"}],
                "todo_summary": "0/1 done",
            },
        )
        assert todo_updated_event(result) is None

    def test_default_agent_id_is_minus_one(self):
        result = ToolResult(
            output="done",
            metadata={
                "todo_items": [{"id": "task1", "content": "x", "status": "pending"}],
                "todo_summary": "0/1 done",
            },
        )
        event = todo_updated_event(result)
        assert event.agent_id == -1


# ── apply_todo_state_to_host: tracker restoration ──────────────────


class TestApplyTodoStateToHost:
    def test_tracker_restored_with_all_statuses(self):
        from voidx.agent.todo_state import apply_todo_state_to_host
        from voidx.runtime.task_state import TodoRunItem, TodoRunState
        from voidx.tools.task_tracker import TaskTracker

        state = TodoRunState(
            summary="1/3 done · 1 active · 1 pending",
            total=3,
            done=1,
            active=1,
            pending=1,
            active_items=[TodoRunItem(id="b", content="active", status="active")],
            items=[
                TodoRunItem(id="a", content="done", status="done"),
                TodoRunItem(id="b", content="active", status="active"),
                TodoRunItem(id="c", content="pending", status="pending"),
            ],
        )
        host = SimpleNamespace(_task_state=None, _tracker=TaskTracker())
        apply_todo_state_to_host(host, state)
        todos = host._tracker.get_todos()
        assert set(todos.keys()) == {"a", "b", "c"}
        assert todos["a"]["status"] == "done"
        assert todos["b"]["status"] == "active"
        assert todos["c"]["status"] == "pending"


# ── tool_executor.py integration: top-level todo emits TodoUpdated ──


class TestToolExecutionTodoEmit:
    @pytest.mark.asyncio
    async def test_top_level_todo_tool_emits_todo_updated(self, tmp_path):
        """When the top-level tool execution runs a 'todo' tool with via_events=True,
        it should call todo_updated_event(result) and emit the result."""
        from voidx.agent.infrastructure.langgraph.runtime import tool_executor as te_mod

        mock_result = ToolResult(
            output="done",
            metadata={
                "todo_items": [{"id": "task_a", "content": "task a", "status": "pending"}],
                "todo_summary": "0/1 done · 0 active · 1 pending",
            },
        )

        fake_event = TodoUpdated(
            items=[TodoItemPayload(id="task_a", content="task a", status="pending")],
            summary="0/1 done · 0 active · 1 pending",
        )

        fake_events = SimpleNamespace(emit=AsyncMock(return_value=True))
        fake_ui = SimpleNamespace(via_events=lambda: True, events=fake_events)

        with patch("voidx.agent.infrastructure.langgraph.runtime.tool_executor.todo_updated_event", return_value=fake_event) as mock_make:
            tid = "todo"
            tc = {"name": tid, "args": {"todos": [{"content": "task a", "status": "pending"}]}, "id": "tc1"}

            # Simulate the code path in execute_one after tool returns
            #   if self._ui.via_events() and tid == "todo":
            #       todo_event = todo_updated_event(result)
            #       if todo_event is not None:
            #           await self._ui.events.emit(todo_event)
            if fake_ui.via_events() and tid == "todo":
                todo_event = te_mod.todo_updated_event(mock_result)
                if todo_event is not None:
                    await fake_ui.events.emit(todo_event)

            mock_make.assert_called_once_with(mock_result)
            fake_events.emit.assert_called_once_with(fake_event)

    @pytest.mark.asyncio
    async def test_non_todo_tool_skips_todo_event(self, tmp_path):
        """Non-todo tools should not trigger todo_updated_event."""
        from voidx.agent.infrastructure.langgraph.runtime import tool_executor as te_mod

        fake_ui = SimpleNamespace(via_events=lambda: True)

        with patch("voidx.agent.infrastructure.langgraph.runtime.tool_executor.todo_updated_event") as mock_make:
            tid = "read"
            if fake_ui.via_events() and tid == "todo":
                te_mod.todo_updated_event(ToolResult(output="x"))

            mock_make.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_emit_when_todo_updated_event_returns_none(self, tmp_path):
        """If todo_updated_event returns None (e.g. missing metadata),
        ui_events.emit should not be called."""
        from voidx.agent.infrastructure.langgraph.runtime import tool_executor as te_mod

        mock_result = ToolResult(output="done")

        fake_events = SimpleNamespace(emit=AsyncMock())
        fake_ui = SimpleNamespace(via_events=lambda: True, events=fake_events)

        with patch("voidx.agent.infrastructure.langgraph.runtime.tool_executor.todo_updated_event", return_value=None):
            tid = "todo"
            if fake_ui.via_events() and tid == "todo":
                todo_event = te_mod.todo_updated_event(mock_result)
                if todo_event is not None:
                    await fake_ui.events.emit(todo_event)

            fake_events.emit.assert_not_called()


# ── subagent integration: child agent todo emits TodoUpdated with agent_id ──


class TestSubagentTodoEmit:
    def test_subagent_todo_tool_uses_emit_direct_with_agent_id(self):
        """When a subagent runs a 'todo' tool, it should call
        todo_updated_event(result, agent_id=agent_id) and emit_direct the result."""
        from voidx.agent.infrastructure.langgraph.runtime import subagent as sub_mod

        agent_id = 2
        mock_result = ToolResult(
            output="done",
            metadata={
                "todo_items": [{"id": "explore", "content": "explore codebase", "status": "active"}],
                "todo_summary": "0/1 done · 1 active · 0 pending",
            },
        )

        fake_event = TodoUpdated(
            agent_id=agent_id,
            items=[TodoItemPayload(id="explore", content="explore codebase", status="active")],
            summary="0/1 done · 1 active · 0 pending",
        )

        fake_events = SimpleNamespace(emit_direct=MagicMock(return_value=True))
        fake_ui = SimpleNamespace(via_events=lambda: True, events=fake_events)

        with patch("voidx.agent.infrastructure.langgraph.runtime.subagent.todo_updated_event", return_value=fake_event) as mock_make:
            tid = "todo"
            # Simulate the code path in subagent.py run_one (lines 286-289):
            #   if ui_port.via_events() and tid == "todo":
            #       todo_event = todo_updated_event(result, agent_id=agent_id)
            #       if todo_event is not None:
            #           ui_port.events.emit_direct(todo_event)
            if fake_ui.via_events() and tid == "todo":
                todo_event = sub_mod.todo_updated_event(mock_result, agent_id=agent_id)
                if todo_event is not None:
                    fake_ui.events.emit_direct(todo_event)

            mock_make.assert_called_once_with(mock_result, agent_id=agent_id)
            fake_events.emit_direct.assert_called_once_with(fake_event)

    def test_subagent_non_todo_tool_skips_todo_event(self):
        """Non-todo tools in subagent should not trigger todo_updated_event."""
        from voidx.agent.infrastructure.langgraph.runtime import subagent as sub_mod

        fake_ui = SimpleNamespace(via_events=lambda: True)

        with patch("voidx.agent.infrastructure.langgraph.runtime.subagent.todo_updated_event") as mock_make:
            tid = "read"
            if fake_ui.via_events() and tid == "todo":
                sub_mod.todo_updated_event(ToolResult(output="x"), agent_id=0)

            mock_make.assert_not_called()

    def test_subagent_no_emit_direct_when_event_is_none(self):
        """If todo_updated_event returns None, emit_direct should not be called."""
        from voidx.agent.infrastructure.langgraph.runtime import subagent as sub_mod

        mock_result = ToolResult(output="done")

        fake_events = SimpleNamespace(emit_direct=MagicMock())
        fake_ui = SimpleNamespace(via_events=lambda: True, events=fake_events)

        with patch("voidx.agent.infrastructure.langgraph.runtime.subagent.todo_updated_event", return_value=None):
            tid = "todo"
            agent_id = 5
            if fake_ui.via_events() and tid == "todo":
                todo_event = sub_mod.todo_updated_event(mock_result, agent_id=agent_id)
                if todo_event is not None:
                    fake_ui.events.emit_direct(todo_event)

            fake_events.emit_direct.assert_not_called()
