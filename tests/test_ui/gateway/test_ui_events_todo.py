import asyncio
import re
import sys
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from voidx.ui.output.capture import CaptureConsole
from voidx.ui.output.console import StreamingRenderer
from voidx.ui.output.dock import ANSI_LINE_PREFIX, BottomInputDock, set_dock
from voidx.ui.output.display_policy import ToolDisplayMode
from voidx.ui.output.events import (
    AssistantStreamCommitted,
    AssistantStreamUpdated,
    DockEventConsumer,
    ErrorAppended,
    FileChangeAppended,
    GuidanceSubmitted,
    PermissionPromptCleared,
    PermissionPromptShown,
    PermissionToolDetail,
    StartupShown,
    StatusFinished,
    StatusUpdated,
    SubagentFinished,
    SubagentStarted,
    SubagentStepStarted,
    ToolFinished,
    ToolResultAppended,
    ToolStarted,
    TodoCleared,
    TodoCommitted,
    TodoItemPayload,
    TodoUpdated,
    TurnStarted,
    UiEventBus,
    ui_events,
)
from voidx.ui.output.tree import OutputTree

from tests.test_ui.gateway.conftest import _plain, _rich_plain, _tree_nodes, isolated_dock

@pytest.mark.asyncio
async def test_todo_updated_sets_pinned_state_until_committed(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.request(ToolStarted(
            tool_call_id="todo_call",
            tool_name="todo",
            label="Updating",
            args="",
            display_mode=ToolDisplayMode.HIDDEN,
        ))
        await bus.emit(TodoUpdated(
            items=[
                TodoItemPayload(id="impl", content="implement event", status="active"),
                TodoItemPayload(id="test", content="write tests", status="pending"),
            ],
            summary="0/2 done · 1 active · 1 pending",
        ))
        await bus.emit(TodoUpdated(
            items=[
                TodoItemPayload(id=f"task_{idx}", content=f"task {idx}", status="pending")
                for idx in range(10)
            ],
            summary="0/10 done · 0 active · 10 pending",
        ))
        await bus.emit(ToolResultAppended(
            tool_call_id="todo_call",
            text="Todo: 0/10 done · 0 active · 10 pending",
        ))
        await bus.emit(ToolFinished(
            tool_call_id="todo_call",
            label="Todo",
            elapsed=0.1,
            ok=True,
        ))
        await bus.drain()

        todo_nodes = [node for node in isolated_dock.tree.root.children if node.node_type == "todo"]
        tool_nodes = [
            node
            for node in _tree_nodes(isolated_dock.tree.root)
            if node.node_type in {"tool_call", "tool_result"}
        ]
        state = isolated_dock.todo_state()

        assert state is not None
        assert state.summary == "0/10 done · 0 active · 10 pending"
        assert len(state.items) == 10
        assert todo_nodes == []
        assert tool_nodes == []

        await bus.emit(TodoCommitted())
        await bus.drain()

        todo_nodes = [node for node in isolated_dock.tree.root.children if node.node_type == "todo"]
        rendered = "\n".join(_rich_plain(line) for line in isolated_dock.tree.render(120))

        assert isolated_dock.todo_state() is None
        assert len(todo_nodes) == 1
        assert isolated_dock.tree.root.children[-1] is todo_nodes[0]
        assert todo_nodes[0].payload["summary"] == "0/10 done · 0 active · 10 pending"
        assert len(todo_nodes[0].payload["items"]) == 10
        assert "Todo: 0/10 done" in rendered
        assert "task 7" in rendered
        assert "2 more todos" in rendered
        assert "task 8" not in rendered
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_todo_updated_sets_pinned_todo_state(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(TodoUpdated(
            items=[
                TodoItemPayload(id="impl", content="implement pinned display", status="active"),
                TodoItemPayload(id="test", content="write tests", status="pending"),
            ],
            summary="0/2 done · 1 active · 1 pending",
        ))
        await bus.drain()

        state = isolated_dock.todo_state()
        todo_nodes = [node for node in isolated_dock.tree.root.children if node.node_type == "todo"]

        assert state is not None
        assert state.summary == "0/2 done · 1 active · 1 pending"
        assert [(item.content, item.status) for item in state.items] == [
            ("implement pinned display", "active"),
            ("write tests", "pending"),
        ]
        assert todo_nodes == []
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_todo_cleared_removes_pinned_todo_without_transcript_node(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(TodoUpdated(
            items=[TodoItemPayload(id="temp", content="temporary task", status="active")],
            summary="0/1 done · 1 active · 0 pending",
        ))
        await bus.emit(TodoCleared())
        await bus.drain()

        assert isolated_dock.todo_state() is None
        assert not any(node.node_type == "todo" for node in isolated_dock.tree.root.children)
    finally:
        await bus.stop()


def test_restore_tree_does_not_hydrate_committed_todo_state(isolated_dock):
    tree = OutputTree()
    tree.new_node(
        parent=tree.root,
        node_type="todo",
        header="Todo",
        collapsed=False,
        status="done",
        payload={
            "summary": "1/2 done · 0 active · 1 pending",
            "items": [
                {"content": "done task", "status": "done"},
                {"content": "next task", "status": "pending"},
            ],
        },
    )
    tree.new_node(
        parent=tree.root,
        node_type="todo",
        header="Todo",
        collapsed=False,
        status="done",
        payload={
            "summary": "stale todo",
            "items": [
                {"content": "stale task", "status": "pending"},
            ],
        },
    )

    isolated_dock.restore_tree(tree)

    assert isolated_dock.todo_state() is None


def test_reset_clears_pinned_todo_state(isolated_dock):
    isolated_dock.set_todo_state(
        "0/1 done · 1 active · 0 pending",
        [{"content": "active task", "status": "active"}],
    )

    isolated_dock.reset()

    assert isolated_dock.todo_state() is None


@pytest.mark.asyncio
async def test_todo_committed_appends_each_turn_todo_and_clears_pinned(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="first"))
        await bus.emit(TodoUpdated(
            items=[TodoItemPayload(id="first", content="first task", status="pending")],
            summary="0/1 done · 0 active · 1 pending",
        ))
        await bus.emit(TodoCommitted())
        await bus.request(TurnStarted(text="second"))
        await bus.emit(TodoUpdated(
            items=[TodoItemPayload(id="second", content="second task", status="active")],
            summary="0/1 done · 1 active · 0 pending",
        ))
        await bus.emit(TodoCommitted())
        await bus.drain()

        todo_nodes = [node for node in isolated_dock.tree.root.children if node.node_type == "todo"]

        assert isolated_dock.todo_state() is None
        assert len(todo_nodes) == 2
        assert todo_nodes[0].payload["summary"] == "0/1 done · 0 active · 1 pending"
        assert todo_nodes[0].payload["items"] == [
            {"id": "first", "content": "first task", "status": "pending"}
        ]
        assert isolated_dock.tree.root.children[-1] is todo_nodes[1]
        assert todo_nodes[1].payload["summary"] == "0/1 done · 1 active · 0 pending"
        assert todo_nodes[1].payload["items"] == [
            {"id": "second", "content": "second task", "status": "active"}
        ]
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_todo_updated_preserves_existing_root_order_until_commit(isolated_dock):
    isolated_dock.begin_capture()
    root = isolated_dock.tree.root
    existing = isolated_dock.tree.new_node(
        parent=root,
        node_type="message",
        header="older output",
        collapsed=False,
        status="done",
    )
    todo = isolated_dock.tree.new_node(
        parent=root,
        node_type="todo",
        header="old todo",
        collapsed=False,
        status="done",
    )
    assert root.children == [existing, todo]

    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.emit(TodoUpdated(
            items=[TodoItemPayload(id="front", content="front task", status="pending")],
            summary="0/1 done · 0 active · 1 pending",
        ))
        await bus.drain()

        assert root.children == [existing, todo]
        assert isolated_dock.todo_state() is not None

        await bus.emit(TodoCommitted())
        await bus.drain()

        todo_nodes = [node for node in root.children if node.node_type == "todo"]
        assert root.children[:2] == [existing, todo]
        assert root.children[-1] is todo_nodes[-1]
        assert todo_nodes[-1].payload["summary"] == "0/1 done · 0 active · 1 pending"
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_todo_updated_with_agent_id_updates_global_root_todo(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.request(ToolStarted(
            agent_id=-1,
            tool_call_id="task_call",
            tool_name="agent",
            label="Running",
            args='agent="explore"',
        ))
        await bus.emit(SubagentStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="explore",
            description="inspect project",
            parent_agent_id=-1,
            parent_tool_call_id="task_call",
        ))
        await bus.emit(TodoUpdated(
            agent_id=0,
            items=[TodoItemPayload(id="auth", content="inspect auth", status="active")],
            summary="0/1 done · 1 active · 0 pending",
        ))
        await bus.drain()

        assistant = next(node for node in isolated_dock.tree.root.children if node.node_type == "assistant")
        subagent = next(node for node in assistant.children if node.node_type == "subagent")

        assert not any(node.node_type == "todo" for node in isolated_dock.tree.root.children)
        await bus.emit(TodoCommitted())
        await bus.drain()

        todo = next(node for node in isolated_dock.tree.root.children if node.node_type == "todo")

        assert isolated_dock.tree.root.children[-1] is todo
        assert todo.payload["items"] == [{"id": "auth", "content": "inspect auth", "status": "active"}]
        assert not any(node.node_type == "todo" for node in assistant.children)
        assert not any(node.node_type == "todo" for node in subagent.children)
    finally:
        await bus.stop()
