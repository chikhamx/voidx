import asyncio
import re
import sys
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text


from voidx.presentation.output.capture import CaptureConsole
from voidx.presentation.output.console import StreamingRenderer
from voidx.presentation.output.dock import ANSI_LINE_PREFIX, BottomInputDock, set_dock
from voidx.presentation.output.display_policy import ToolDisplayMode
from voidx.presentation.output.events import (
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
from voidx.presentation.output.tree import OutputTree

from tests.test_presentation.gateway.conftest import _plain, _rich_plain, _tree_nodes, isolated_dock

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
        assert "Todo: 0/10 done" not in rendered
        assert "task 7" not in rendered
        assert "2 more todos" not in rendered
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
async def test_todo_updated_with_agent_id_stays_under_subagent(isolated_dock):
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
        todo = next(node for node in subagent.children if node.node_type == "todo")

        rendered = "\n".join(_rich_plain(line) for line in isolated_dock.tree.render(120))

        assert isolated_dock.todo_state() is None
        assert not any(node.node_type == "todo" for node in isolated_dock.tree.root.children)
        assert todo.status == "done"
        assert todo.payload["items"] == [{"id": "auth", "content": "inspect auth", "status": "active"}]
        assert "└─ Todo: 0/1 done · 1 active · 0 pending" in rendered
        assert "◐ auth: inspect auth" in rendered

        await bus.emit(TodoCommitted())
        await bus.drain()

        assert isolated_dock.todo_state() is None
        assert not any(node.node_type == "todo" for node in isolated_dock.tree.root.children)
        assert todo in subagent.children
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_child_todo_preserves_parent_pin_and_empty_write_clears_only_child(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.emit(TodoUpdated(
            items=[TodoItemPayload(id="parent", content="parent work", status="active")],
            summary="0/1 done · 1 active · 0 pending",
        ))
        await bus.emit(SubagentStarted(
            agent_id=0,
            subagent_id="agent_0",
            name="explore",
            description="inspect project",
        ))
        await bus.emit(TodoUpdated(
            agent_id=0,
            items=[TodoItemPayload(id="child", content="child work", status="pending")],
            summary="0/1 done · 0 active · 1 pending",
        ))
        await bus.drain()

        parent_state = isolated_dock.todo_state()
        subagent = next(node for node in _tree_nodes(isolated_dock.tree.root) if node.node_type == "subagent")
        assert parent_state is not None
        assert parent_state.items[0].id == "parent"
        assert [child.node_type for child in subagent.children].count("todo") == 1

        await bus.emit(TodoUpdated(
            agent_id=0,
            items=[],
            summary="0/0 done · 0 active · 0 pending",
        ))
        await bus.drain()

        assert isolated_dock.todo_state() == parent_state
        assert not any(child.node_type == "todo" for child in subagent.children)
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_two_children_keep_independent_todo_nodes(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    consumer = DockEventConsumer(isolated_dock)
    bus.start(consumer)
    try:
        await bus.request(TurnStarted(text="demo"))
        for agent_id, item_id in ((0, "alpha"), (1, "beta")):
            await bus.emit(SubagentStarted(
                agent_id=agent_id,
                subagent_id=f"agent_{agent_id}",
                name="explore",
                description=f"inspect {item_id}",
            ))
            await bus.emit(TodoUpdated(
                agent_id=agent_id,
                items=[TodoItemPayload(id=item_id, content=f"{item_id} work", status="active")],
                summary="0/1 done · 1 active · 0 pending",
            ))
        await bus.drain()

        subagents = {
            node.payload["agent_id"]: node
            for node in _tree_nodes(isolated_dock.tree.root)
            if node.node_type == "subagent"
        }
        assert isolated_dock.todo_state() is None
        assert set(subagents) == {0, 1}
        assert subagents[0].children[0].payload["items"][0]["id"] == "alpha"
        assert subagents[1].children[0].payload["items"][0]["id"] == "beta"
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_subagent_started_migrates_fallback_todo_to_canonical_tool_node(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    bus.start(DockEventConsumer(isolated_dock))
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.request(ToolStarted(
            tool_call_id="task_call",
            tool_name="agent",
            label="Running",
            args='agent="explore"',
        ))
        await bus.emit(TodoUpdated(
            agent_id=0,
            items=[TodoItemPayload(id="inspect", content="inspect project", status="active")],
            summary="0/1 done · 1 active · 0 pending",
        ))
        await bus.drain()

        fallback = next(node for node in _tree_nodes(isolated_dock.tree.root) if node.node_type == "subagent")
        assert any(child.node_type == "todo" for child in fallback.children)

        await bus.emit(SubagentStarted(
            agent_id=0,
            subagent_id="run-0",
            name="explore",
            description="inspect project",
            parent_tool_call_id="task_call",
        ))
        await bus.drain()

        subagents = [node for node in _tree_nodes(isolated_dock.tree.root) if node.node_type == "subagent"]
        assert len(subagents) == 1
        assert subagents[0].agent_run_id == "run-0"
        assert subagents[0].payload["description"] == "inspect project"
        assert [child.node_type for child in subagents[0].children] == ["todo"]
        todo = subagents[0].children[0]
        assert todo.payload["items"][0]["id"] == "inspect"
        assert todo.parent is subagents[0]
        assert todo.depth == subagents[0].depth + 1
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_subagent_started_recomputes_migrated_fallback_subtree_depths(isolated_dock):
    isolated_dock.begin_capture()
    bus = UiEventBus()
    consumer = DockEventConsumer(isolated_dock)
    bus.start(consumer)
    try:
        await bus.request(TurnStarted(text="demo"))
        await bus.emit(TodoUpdated(
            agent_id=0,
            items=[TodoItemPayload(id="inspect", content="inspect project", status="active")],
            summary="0/1 done · 1 active · 0 pending",
        ))
        await bus.drain()

        fallback = next(
            node
            for node in _tree_nodes(isolated_dock.tree.root)
            if node.node_type == "subagent" and node.agent_name == "agent 0"
        )
        todo = next(child for child in fallback.children if child.node_type == "todo")
        todo_detail = isolated_dock.tree.new_node(
            parent=todo,
            node_type="message",
            header="todo detail",
            status="done",
        )
        assistant = fallback.parent
        assert assistant is not None
        canonical_parent = isolated_dock.tree.new_node(
            parent=assistant,
            node_type="subagent",
            header="outer child",
            payload={"agent_id": 9},
        )
        canonical = isolated_dock.tree.new_node(
            parent=canonical_parent,
            node_type="tool_call",
            header="nested agent tool",
            tool_call_id="nested_agent_call",
            payload={"tool_name": "agent"},
        )
        existing = isolated_dock.tree.new_node(
            parent=canonical,
            node_type="message",
            header="existing child",
            status="done",
        )
        consumer._tool_nodes["nested_agent_call"] = canonical
        assert todo.depth != canonical.depth + 1

        await bus.emit(SubagentStarted(
            agent_id=0,
            subagent_id="run-0",
            name="explore",
            description="inspect project",
            parent_tool_call_id="nested_agent_call",
        ))
        await bus.drain()

        assert canonical.node_type == "subagent"
        assert canonical.children == [existing, todo]
        assert todo.parent is canonical
        assert todo.depth == canonical.depth + 1
        assert todo_detail.parent is todo
        assert todo_detail.depth == todo.depth + 1
        assert existing._is_last_sibling is False
        assert todo._is_last_sibling is True
    finally:
        await bus.stop()
