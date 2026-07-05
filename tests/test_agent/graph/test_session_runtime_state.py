"""Tests for runtime state persistence and snapshots."""

import json
import sys
from pathlib import Path

from tests.test_agent.conftest import _read_jsonl, _session_dir, _table_names

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import voidx.memory.store as store
import voidx.memory.jsonl_store as jsonl_store

from voidx.agent.runtime_context import InteractionMode, TaskIntent
from voidx.agent.task_state import (
    GoalSpec,
    TaskState,
    TodoRunState,
    TurnExchange,
    WorkflowRoute,
)
from voidx.memory.session import (
    create_session,
    get_session,
    delete_session,
    save_message,
    load_messages,
    delete_messages_from,
    delete_messages_through,
    clear_messages,
    MessageRow,
)
from voidx.memory.context_frames import (
    build_context_frame,
    load_context_frames,
    save_context_frame,
)
from voidx.memory.jsonl_store import append_session_record
from voidx.memory.runtime_state import (
    MessageRuntimeSnapshot,
    RuntimeStateSnapshot,
    clear_runtime_state,
    load_message_runtime_snapshot,
    load_runtime_state,
    save_message_runtime_snapshot,
    save_runtime_state,
)
from voidx.memory.transcript import (
    TranscriptNodeRow,
    replace_transcript,
    load_transcript,
)
from voidx.workflow.runtime import WorkflowActivationSource, WorkflowRunState, WorkflowRunStatus

@pytest.mark.asyncio
async def test_runtime_state_round_trips_structured_goal_state():
    session = await create_session()
    try:
        state = TaskState(
            current_intent=TaskIntent.CODING,
            previous_intent=TaskIntent.CODING,
            current_goal=GoalSpec(desc="优化 markdown 渲染截断"),
            workflow_route=WorkflowRoute(join="brainstorm", leave="plan"),
            recent_exchanges=[
                TurnExchange(user_text="看看现状", assistant_text="现状如下"),
                TurnExchange(user_text="给个方案", assistant_text="方案如下"),
            ],
            workflow_runs={
                "brainstorm": WorkflowRunState(
                    name="brainstorm",
                    status=WorkflowRunStatus.ACTIVE,
                    source=WorkflowActivationSource.WORKFLOW,
                    reason="goal:design",
                    goal_type="design",
                    scope="优化 markdown 渲染截断",
                )
            },
            todo_state=TodoRunState.model_validate({
                "summary": "0/2 done · 1 active · 1 pending",
                "total": 2,
                "done": 0,
                "active": 1,
                "pending": 1,
                "active_items": [
                    {"id": "inspect", "content": "inspect current behavior", "status": "active"},
                ],
                "updated_at": "2026-06-11T00:00:00+00:00",
            }),
        )

        await save_runtime_state(
            session.id,
            RuntimeStateSnapshot(
                interaction_mode=InteractionMode.GOAL,
                task_state=state,
                session_time="2026-06-11 CST",
            ),
        )

        loaded = await load_runtime_state(session.id)

        assert loaded.interaction_mode == InteractionMode.GOAL
        assert loaded.session_time == "2026-06-11 CST"
        assert loaded.task_state.current_intent == TaskIntent.CODING
        assert loaded.task_state.previous_intent == TaskIntent.CODING
        assert loaded.task_state.current_goal is not None
        assert loaded.task_state.current_goal.desc == "优化 markdown 渲染截断"
        assert loaded.task_state.current_goal.desc == "优化 markdown 渲染截断"
        assert loaded.task_state.workflow_route is not None
        assert loaded.task_state.workflow_route.join == "brainstorm"
        assert loaded.task_state.workflow_route.leave == "plan"
        assert loaded.task_state.recent_exchanges == []
        assert loaded.task_state.todo_state is not None
        assert loaded.task_state.todo_state.summary == "0/2 done · 1 active · 1 pending"
        assert loaded.task_state.todo_state.active_items[0].content == "inspect current behavior"
        assert loaded.task_state.workflow_runs["brainstorm"].status == WorkflowRunStatus.ACTIVE
        assert loaded.task_state.workflow_runs["brainstorm"].goal_type == "design"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_runtime_state_empty_todo_persists_as_cleared_state():
    session = await create_session()
    try:
        await save_runtime_state(
            session.id,
            RuntimeStateSnapshot(
                interaction_mode=InteractionMode.AUTO,
                task_state=TaskState(
                    todo_state=TodoRunState.model_validate({
                        "summary": "0/0 done · 0 active · 0 pending",
                        "items": [],
                    }),
                ),
            ),
        )

        loaded = await load_runtime_state(session.id)

        assert loaded.task_state.todo_state is None
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_message_runtime_snapshot_round_trips_per_turn_state():
    session = await create_session()
    try:
        message_id = await save_message(MessageRow(session_id=session.id, role="user", content="给个方案"))
        await save_message_runtime_snapshot(MessageRuntimeSnapshot(
            message_id=message_id,
            session_id=session.id,
            interaction_mode=InteractionMode.GOAL,
            task_intent=TaskIntent.CODING,
            current_goal=GoalSpec(desc="优化 markdown 渲染截断"),
            workflow_route=WorkflowRoute(join="brainstorm", leave="plan"),
            workflow_runs={
                "brainstorm": WorkflowRunState(
                    name="brainstorm",
                    status=WorkflowRunStatus.ACTIVE,
                    reason="goal:design",
                    goal_type="design",
                )
            },
        ))

        loaded = await load_message_runtime_snapshot(message_id)

        assert loaded is not None
        assert loaded.interaction_mode == InteractionMode.GOAL
        assert loaded.task_intent == TaskIntent.CODING
        assert loaded.current_goal is not None
        assert loaded.current_goal.desc == "优化 markdown 渲染截断"
        assert loaded.workflow_route is not None
        assert loaded.workflow_route.join == "brainstorm"
        assert loaded.workflow_route.leave == "plan"
        assert loaded.workflow_runs["brainstorm"].goal_type == "design"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_message_runtime_snapshot_round_trips_from_runtime_debug_jsonl():
    session = await create_session()
    try:
        message_id = await save_message(MessageRow(session_id=session.id, role="user", content="给个方案"))
        await save_message_runtime_snapshot(MessageRuntimeSnapshot(
            message_id=message_id,
            session_id=session.id,
            interaction_mode=InteractionMode.GOAL,
            task_intent=TaskIntent.CODING,
            current_goal=GoalSpec(desc="优化 markdown 渲染截断"),
            workflow_route=WorkflowRoute(join="brainstorm", leave="plan"),
            workflow_runs={
                "brainstorm": WorkflowRunState(
                    name="brainstorm",
                    status=WorkflowRunStatus.ACTIVE,
                    reason="goal:design",
                    goal_type="design",
                )
            },
        ))
        rows = _read_jsonl(_session_dir(session.id) / "runtime_debug.jsonl")
        loaded = await load_message_runtime_snapshot(message_id)

        assert rows[-1]["type"] == "message_runtime_snapshot"
        assert rows[-1]["message_id"] == message_id
        assert loaded is not None
        assert loaded.interaction_mode == InteractionMode.GOAL
        assert loaded.current_goal is not None
        assert loaded.current_goal.desc == "优化 markdown 渲染截断"
        assert loaded.workflow_route is not None
        assert loaded.workflow_route.join == "brainstorm"
        assert loaded.workflow_runs["brainstorm"].goal_type == "design"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_message_runtime_snapshot_does_not_write_legacy_snapshot_table():
    session = await create_session()
    try:
        message_id = await save_message(MessageRow(session_id=session.id, role="user", content="给个方案"))
        await save_message_runtime_snapshot(MessageRuntimeSnapshot(
            message_id=message_id,
            session_id=session.id,
            interaction_mode=InteractionMode.GOAL,
            task_intent=TaskIntent.CODING,
            current_goal=GoalSpec(desc="优化 markdown 渲染截断"),
        ))

        tables = await _table_names()
        loaded = await load_message_runtime_snapshot(message_id)

        assert "message_runtime_snapshots" not in tables
        assert loaded is not None
        assert loaded.message_id == message_id
        assert loaded.current_goal is not None
        assert loaded.current_goal.desc == "优化 markdown 渲染截断"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_message_runtime_snapshot_updates_latest_session_runtime_state():
    session = await create_session()
    try:
        message_id = await save_message(MessageRow(session_id=session.id, role="user", content="给个方案"))
        await save_message_runtime_snapshot(MessageRuntimeSnapshot(
            message_id=message_id,
            session_id=session.id,
            interaction_mode=InteractionMode.GOAL,
            task_intent=TaskIntent.CODING,
            current_goal=GoalSpec(desc="优化 markdown 渲染截断"),
            workflow_route=WorkflowRoute(join="brainstorm", leave="plan"),
            workflow_runs={
                "brainstorm": WorkflowRunState(
                    name="brainstorm",
                    status=WorkflowRunStatus.ACTIVE,
                    reason="goal:design",
                    goal_type="design",
                )
            },
        ))

        loaded = await load_runtime_state(session.id)

        assert loaded.interaction_mode == InteractionMode.GOAL
        assert loaded.task_state.current_intent == TaskIntent.CODING
        assert loaded.task_state.current_goal is not None
        assert loaded.task_state.current_goal.desc == "优化 markdown 渲染截断"
        assert loaded.task_state.workflow_route is not None
        assert loaded.task_state.workflow_route.join == "brainstorm"
        assert loaded.task_state.workflow_runs["brainstorm"].goal_type == "design"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_delete_session_cascades_all_child_tables():
    """delete_session should cascade-delete rows in ALL child tables via FK."""
    session = await create_session()
    try:
        msg_id = await save_message(MessageRow(session_id=session.id, role="user", content="x"))
        await replace_transcript(
            session.id,
            [TranscriptNodeRow(session_id=session.id, turn_id=0, node_id=0, sort_order=0, node_type="turn", header="hello")],
            turn_count=1,
        )
        await save_context_frame(build_context_frame(
            session_id=session.id,
            user_message_id=msg_id,
            provider="mimo",
            model="mimo-v2.5",
            messages=[SystemMessage(content="VOIDX_RUNTIME_CONTEXT"), HumanMessage(content="x")],
        ))
        await save_runtime_state(session.id, RuntimeStateSnapshot(
            interaction_mode=InteractionMode.GOAL,
            task_state=TaskState(current_goal=GoalSpec(desc="test goal")),
        ))
        await save_message_runtime_snapshot(MessageRuntimeSnapshot(
            message_id=msg_id,
            session_id=session.id,
            interaction_mode=InteractionMode.GOAL,
            task_intent=TaskIntent.CODING,
        ))

        await delete_session(session.id)

        assert await load_messages(session.id) == []
        assert await load_transcript(session.id) == []
        assert await load_context_frames(session.id) == []
        assert await get_session(session.id) is None
        loaded_state = await load_runtime_state(session.id)
        assert loaded_state.interaction_mode == InteractionMode.AUTO
        assert loaded_state.task_state.current_goal is None
        assert await load_message_runtime_snapshot(msg_id) is None
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_clear_messages_cascades_runtime_state():
    """clear_messages should also clear session_runtime_state."""
    session = await create_session()
    try:
        msg_id = await save_message(MessageRow(session_id=session.id, role="user", content="test"))
        await save_runtime_state(session.id, RuntimeStateSnapshot(
            interaction_mode=InteractionMode.GOAL,
            task_state=TaskState(current_goal=GoalSpec(desc="test goal")),
        ))
        await save_message_runtime_snapshot(MessageRuntimeSnapshot(
            message_id=msg_id,
            session_id=session.id,
            interaction_mode=InteractionMode.GOAL,
        ))

        await clear_messages(session.id)

        msgs = await load_messages(session.id)
        assert len(msgs) == 0
        loaded_state = await load_runtime_state(session.id)
        assert loaded_state.interaction_mode == InteractionMode.AUTO
        assert loaded_state.task_state.current_goal is None
        assert await load_message_runtime_snapshot(msg_id) is None
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_clear_runtime_state_resets_structured_state():
    session = await create_session()
    try:
        await save_runtime_state(
            session.id,
            RuntimeStateSnapshot(
                interaction_mode=InteractionMode.GOAL,
                task_state=TaskState(current_goal=GoalSpec(desc="修复 UI")),
            ),
        )

        await clear_runtime_state(session.id)
        loaded = await load_runtime_state(session.id)

        assert loaded.interaction_mode == InteractionMode.AUTO
        assert loaded.task_state.current_goal is None
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_graph_session_runtime_persists_and_restores_structured_state():
    from types import SimpleNamespace

    from voidx.agent.graph.session_runtime import GraphSessionRuntime

    session = await create_session()
    try:
        host = SimpleNamespace(
            _session=session,
            _interaction_mode=InteractionMode.GOAL,
            _task_state=TaskState(current_goal=GoalSpec(desc="ship 5B")),
            _compaction_summary="summary",
            _session_date="2026-06-11 CST",
        )

        runtime = GraphSessionRuntime(host)
        await runtime.persist_runtime_state()

        host._interaction_mode = InteractionMode.AUTO
        host._task_state = TaskState()
        host._compaction_summary = ""
        host._session_date = ""

        await runtime.restore_runtime_state()

        assert host._interaction_mode == InteractionMode.GOAL
        assert host._task_state.current_goal is not None
        assert host._task_state.current_goal.desc == "ship 5B"
        assert host._compaction_summary == "summary"
        assert host._session_date == "2026-06-11 CST"
    finally:
        await delete_session(session.id)
