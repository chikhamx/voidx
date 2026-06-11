"""Tests for session persistence layer."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from voidx.agent.message_rows import messages_from_rows, messages_from_rows_incremental
from voidx.agent.runtime_context import InteractionMode, TaskIntent
from voidx.agent.task_state import PendingApproval, TaskPhase, TaskRun, TaskRunStatus, TaskState
from voidx.memory.context_frames import (
    build_context_frame,
    load_context_frames,
    save_context_frame,
)
from voidx.memory.runtime_state import (
    MessageRuntimeSnapshot,
    RuntimeStateSnapshot,
    clear_runtime_state,
    load_message_runtime_snapshot,
    load_runtime_state,
    save_message_runtime_snapshot,
    save_runtime_state,
)
from voidx.memory.session import (
    create_session,
    get_session,
    list_sessions,
    latest_session_for_workspace,
    delete_session,
    save_message,
    load_messages,
    clear_messages,
    update_title,
    MessageRow,
)
from voidx.memory.transcript import (
    TranscriptNodeRow,
    load_transcript,
    replace_transcript,
)
from voidx.workflow.runtime import WorkflowActivationSource, WorkflowRunState, WorkflowRunStatus


@pytest.mark.asyncio
async def test_create_and_get():
    session = await create_session(workspace="/tmp/test")
    assert session.id
    assert session.title == "New session"

    loaded = await get_session(session.id)
    assert loaded is not None
    assert loaded.id == session.id
    assert loaded.workspace == "/tmp/test"

    await delete_session(session.id)


@pytest.mark.asyncio
async def test_save_and_load_messages():
    session = await create_session()

    await save_message(MessageRow(session_id=session.id, role="user", content="hello"))
    await save_message(MessageRow(session_id=session.id, role="assistant", content="hi there"))
    await save_message(MessageRow(
        session_id=session.id, role="assistant", content="ok",
        tool_calls=[{"name": "read", "args": {"file_path": "x.txt"}, "id": "c1"}],
    ))

    msgs = await load_messages(session.id)
    assert len(msgs) == 3
    assert msgs[0].role == "user"
    assert msgs[0].content == "hello"
    assert msgs[1].role == "assistant"
    assert msgs[1].content == "hi there"
    assert msgs[2].tool_calls is not None
    assert msgs[2].tool_calls[0]["name"] == "read"

    await delete_session(session.id)


def test_messages_from_rows_preserves_content_and_tool_fields():
    rows = [
        MessageRow(id=1, session_id="s", role="system", content="system"),
        MessageRow(
            id=2,
            session_id="s",
            role="user",
            content='[{"type":"text","text":"hello"}]',
            content_format="structured",
        ),
        MessageRow(
            id=3,
            session_id="s",
            role="assistant",
            content="ok",
            tool_calls=[{"name": "read", "args": {"file_path": "x.txt"}, "id": "c1"}],
        ),
        MessageRow(id=4, session_id="s", role="tool", content="result", tool_call_id="c1"),
    ]

    messages = messages_from_rows(rows)

    assert isinstance(messages[0], SystemMessage)
    assert messages[0].id == "1"
    assert isinstance(messages[1], HumanMessage)
    assert messages[1].content == [{"type": "text", "text": "hello"}]
    assert isinstance(messages[2], AIMessage)
    assert messages[2].tool_calls[0]["name"] == "read"
    assert isinstance(messages[3], ToolMessage)
    assert messages[3].tool_call_id == "c1"


def test_messages_from_rows_incremental_reuses_cached_rows():
    rows = [
        MessageRow(id=1, session_id="s", role="user", content="hello"),
        MessageRow(id=2, session_id="s", role="assistant", content="hi"),
    ]

    first, cache = messages_from_rows_incremental(rows, {})
    second, next_cache = messages_from_rows_incremental(rows, cache)

    assert second[0] is first[0]
    assert second[1] is first[1]
    assert set(next_cache) == {1, 2}


def test_messages_from_rows_incremental_rebuilds_changed_row_same_id():
    first, cache = messages_from_rows_incremental([
        MessageRow(id=1, session_id="s", role="tool", content="old", tool_call_id="c1"),
    ], {})

    second, _ = messages_from_rows_incremental([
        MessageRow(id=1, session_id="s", role="tool", content="new", tool_call_id="c1"),
    ], cache)

    assert second[0] is not first[0]
    assert second[0].content == "new"


@pytest.mark.asyncio
async def test_list_sessions():
    s1 = await create_session()
    s2 = await create_session()
    sessions = await list_sessions()
    ids = [s.id for s in sessions]
    assert s1.id in ids
    assert s2.id in ids
    await delete_session(s1.id)
    await delete_session(s2.id)


@pytest.mark.asyncio
async def test_latest_session_for_workspace_returns_newest_matching_workspace(tmp_path):
    workspace = str(tmp_path)
    other_workspace = str(tmp_path / "other")
    older = await create_session(workspace=workspace)
    latest = await create_session(workspace=workspace)
    other = await create_session(workspace=other_workspace)
    await update_title(latest.id, "Latest")
    try:
        loaded = await latest_session_for_workspace(workspace)

        assert loaded is not None
        assert loaded.id == latest.id
        assert loaded.workspace == workspace
    finally:
        await delete_session(older.id)
        await delete_session(latest.id)
        await delete_session(other.id)


@pytest.mark.asyncio
async def test_clear_messages():
    session = await create_session()
    try:
        message_id = await save_message(MessageRow(session_id=session.id, role="user", content="test"))
        await save_context_frame(build_context_frame(
            session_id=session.id,
            user_message_id=message_id,
            provider="mimo",
            model="mimo-v2.5",
            messages=[SystemMessage(content="VOIDX_RUNTIME_CONTEXT"), HumanMessage(content="test")],
        ))

        await clear_messages(session.id)

        msgs = await load_messages(session.id)
        frames = await load_context_frames(session.id)
        assert len(msgs) == 0
        assert frames == []
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_update_title():
    session = await create_session()
    await update_title(session.id, "Custom Title")
    loaded = await get_session(session.id)
    assert loaded is not None
    assert loaded.title == "Custom Title"
    await delete_session(session.id)


@pytest.mark.asyncio
async def test_delete_session_cascades():
    session = await create_session()
    await save_message(MessageRow(session_id=session.id, role="user", content="x"))
    await replace_transcript(
        session.id,
        [
            TranscriptNodeRow(
                session_id=session.id,
                turn_id=0,
                node_id=0,
                sort_order=0,
                node_type="turn",
                header="hello",
            )
        ],
        turn_count=1,
    )
    await delete_session(session.id)

    msgs = await load_messages(session.id)
    assert len(msgs) == 0
    assert await load_transcript(session.id) == []
    assert await get_session(session.id) is None


@pytest.mark.asyncio
async def test_replace_and_load_transcript_nodes():
    session = await create_session()
    try:
        await replace_transcript(
            session.id,
            [
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=0,
                    node_id=0,
                    sort_order=0,
                    node_type="turn",
                    header="question",
                ),
                TranscriptNodeRow(
                    session_id=session.id,
                    turn_id=0,
                    node_id=1,
                    parent_node_id=0,
                    sort_order=1,
                    node_type="thought",
                    header="Thinking",
                    body_lines=["step 1"],
                    collapsed=True,
                    metadata={"meta": "Thinking for 2s"},
                ),
            ],
            turn_count=1,
        )

        rows = await load_transcript(session.id)

        assert [row.node_type for row in rows] == ["turn", "thought"]
        assert rows[1].parent_node_id == 0
        assert rows[1].body_lines == ["step 1"]
        assert rows[1].metadata["meta"] == "Thinking for 2s"
    finally:
        await delete_session(session.id)


def test_context_frame_hashes_stable_prefix_before_long_summary():
    first = build_context_frame(
        session_id="s1",
        provider="mimo",
        model="mimo-v2.5",
        messages=[
            SystemMessage(content=(
                "VOIDX_RUNTIME_CONTEXT\n\n"
                "## Base System\nbase\n\n"
                "## Role Prompt\nrole\n\n"
                "## Session Date\n2026-05-31 CST\n\n"
                "## Long Summary\n- first summary"
            )),
            HumanMessage(content="hi"),
        ],
    )
    second = build_context_frame(
        session_id="s1",
        provider="mimo",
        model="mimo-v2.5",
        messages=[
            SystemMessage(content=(
                "VOIDX_RUNTIME_CONTEXT\n\n"
                "## Base System\nbase\n\n"
                "## Role Prompt\nrole\n\n"
                "## Session Date\n2026-05-31 CST\n\n"
                "## Long Summary\n- second summary"
            )),
            HumanMessage(content="hi"),
        ],
    )

    assert first.prefix_hash == second.prefix_hash
    assert first.frame_hash != second.frame_hash


@pytest.mark.asyncio
async def test_context_frame_round_trips_compiled_messages():
    session = await create_session()
    try:
        message_id = await save_message(MessageRow(session_id=session.id, role="user", content="hello"))
        record = build_context_frame(
            session_id=session.id,
            user_message_id=message_id,
            frame_kind="main",
            agent_role="orchestrator",
            provider="mimo",
            model="mimo-v2.5",
            token_estimate=42,
            metadata={"step": 1},
            messages=[
                SystemMessage(content="VOIDX_RUNTIME_CONTEXT\n\n## Base System\nbase"),
                HumanMessage(content="hello"),
            ],
        )

        frame_id = await save_context_frame(record)
        frames = await load_context_frames(session.id)

        assert frames[0].id == frame_id
        assert frames[0].user_message_id == message_id
        assert frames[0].agent_role == "orchestrator"
        assert frames[0].token_estimate == 42
        assert frames[0].metadata["step"] == 1
        assert frames[0].messages[-1]["content"] == "hello"
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_runtime_state_round_trips_structured_goal_state():
    session = await create_session()
    try:
        state = TaskState(
            current_intent=TaskIntent.DESIGN,
            previous_intent=TaskIntent.INSPECT,
            current_goal="优化 markdown 渲染截断",
            pending_approval=PendingApproval(scope="优化 markdown 渲染截断"),
            last_plan_summary="方案",
            recent_user_texts=["看看现状", "给个方案"],
        )
        run = TaskRun(
            goal="优化 markdown 渲染截断",
            phase=TaskPhase.DESIGN,
            status=TaskRunStatus.ACTIVE,
            pending_approval=PendingApproval(scope="优化 markdown 渲染截断", created_turn=2),
            turn_count=2,
            workflow_runs={
                "brainstorm": WorkflowRunState(
                    name="brainstorm",
                    status=WorkflowRunStatus.ACTIVE,
                    source=WorkflowActivationSource.WORKFLOW,
                    reason="design/create intent",
                    phase="design",
                    scope="优化 markdown 渲染截断",
                    activated_turn=1,
                    updated_turn=2,
                )
            },
        )

        await save_runtime_state(
            session.id,
            RuntimeStateSnapshot(
                interaction_mode=InteractionMode.GOAL,
                task_state=state,
                task_run=run,
            ),
        )

        loaded = await load_runtime_state(session.id)

        assert loaded.interaction_mode == InteractionMode.GOAL
        assert loaded.task_state.current_intent == TaskIntent.DESIGN
        assert loaded.task_state.previous_intent == TaskIntent.INSPECT
        assert loaded.task_state.pending_approval is not None
        assert loaded.task_state.pending_approval.scope == "优化 markdown 渲染截断"
        assert loaded.task_state.recent_user_texts == ["看看现状", "给个方案"]
        assert loaded.task_run.goal == "优化 markdown 渲染截断"
        assert loaded.task_run.phase == TaskPhase.DESIGN
        assert loaded.task_run.turn_count == 2
        assert loaded.task_run.workflow_runs["brainstorm"].status == WorkflowRunStatus.ACTIVE
        assert loaded.task_run.workflow_runs["brainstorm"].phase == "design"
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
            task_intent=TaskIntent.DESIGN,
            intent_resolution_reason="single-turn classifier matched design",
            goal="优化 markdown 渲染截断",
            goal_phase="design",
            goal_status="active",
            goal_turn_count=1,
            pending_approval=PendingApproval(scope="优化 markdown 渲染截断", created_turn=1),
            intent_confidence=0.88,
            intent_source="on_intent",
            intent_refined=True,
            available_tool_ids=["read", "grep"],
        ))

        loaded = await load_message_runtime_snapshot(message_id)

        assert loaded is not None
        assert loaded.interaction_mode == InteractionMode.GOAL
        assert loaded.task_intent == TaskIntent.DESIGN
        assert loaded.goal_phase == "design"
        assert loaded.pending_approval is not None
        assert loaded.pending_approval.scope == "优化 markdown 渲染截断"
        assert loaded.intent_confidence == 0.88
        assert loaded.intent_source == "on_intent"
        assert loaded.intent_refined is True
        assert loaded.available_tool_ids == ["read", "grep"]
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
            task_state=TaskState(current_goal="test goal"),
            task_run=TaskRun(goal="test goal", status=TaskRunStatus.ACTIVE),
        ))
        await save_message_runtime_snapshot(MessageRuntimeSnapshot(
            message_id=msg_id,
            session_id=session.id,
            interaction_mode=InteractionMode.GOAL,
            task_intent=TaskIntent.DESIGN,
        ))

        await delete_session(session.id)

        assert await load_messages(session.id) == []
        assert await load_transcript(session.id) == []
        assert await load_context_frames(session.id) == []
        assert await get_session(session.id) is None
        loaded_state = await load_runtime_state(session.id)
        assert loaded_state.interaction_mode == InteractionMode.AUTO
        assert loaded_state.task_state.current_goal == ""
        assert loaded_state.task_run.status == TaskRunStatus.IDLE
        assert await load_message_runtime_snapshot(msg_id) is None
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_clear_messages_cascades_runtime_state():
    """clear_messages should also clear session_runtime_state and session_task_runs."""
    session = await create_session()
    try:
        msg_id = await save_message(MessageRow(session_id=session.id, role="user", content="test"))
        await save_runtime_state(session.id, RuntimeStateSnapshot(
            interaction_mode=InteractionMode.GOAL,
            task_state=TaskState(current_goal="test goal"),
            task_run=TaskRun(goal="test goal", status=TaskRunStatus.ACTIVE),
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
        assert loaded_state.task_state.current_goal == ""
        assert loaded_state.task_run.status == TaskRunStatus.IDLE
        assert await load_message_runtime_snapshot(msg_id) is None
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_clear_runtime_state_resets_structured_state():
    session = await create_session()
    try:
        run = TaskRun(goal="修复 UI", phase=TaskPhase.INSPECT, status=TaskRunStatus.ACTIVE)
        await save_runtime_state(
            session.id,
            RuntimeStateSnapshot(
                interaction_mode=InteractionMode.GOAL,
                task_state=TaskState(current_goal="修复 UI"),
                task_run=run,
            ),
        )

        await clear_runtime_state(session.id)
        loaded = await load_runtime_state(session.id)

        assert loaded.interaction_mode == InteractionMode.AUTO
        assert loaded.task_state.current_goal == ""
        assert loaded.task_run.status == TaskRunStatus.IDLE
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
            _task_state=TaskState(current_goal="ship 5B"),
            _task_run=TaskRun(goal="ship 5B", phase=TaskPhase.IMPLEMENT, status=TaskRunStatus.ACTIVE),
            _compaction_summary="summary",
        )

        runtime = GraphSessionRuntime(host)
        await runtime.persist_runtime_state()

        host._interaction_mode = InteractionMode.AUTO
        host._task_state = TaskState()
        host._task_run = TaskRun()
        host._compaction_summary = ""

        await runtime.restore_runtime_state()

        assert host._interaction_mode == InteractionMode.GOAL
        assert host._task_state.current_goal == "ship 5B"
        assert host._task_run.goal == "ship 5B"
        assert host._task_run.phase == TaskPhase.IMPLEMENT
        assert host._task_run.status == TaskRunStatus.ACTIVE
        assert host._compaction_summary == "summary"
    finally:
        await delete_session(session.id)
