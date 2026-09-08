from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from tests.langgraph_execution import make_langgraph_execution
from voidx.agent.adapters.langgraph.graph_compaction import GraphCompactionAdapter
from voidx.agent.adapters.langgraph.runtime.session_runtime import SessionRuntime
from voidx.agent.adapters.persistence.message_rows import is_compaction_row
from voidx.agent.adapters.persistence.runtime_state_repository import (
    RuntimeStateSnapshot,
    load_compaction_summary,
    save_runtime_state,
)
from voidx.agent.adapters.persistence.session_repository import (
    MessageRow,
    create_session,
    delete_session,
    load_messages,
    save_message,
)
from voidx.config import Config
from voidx.llm.domain.model import ModelConfig
from voidx.persistence.sqlite import execute_commit


@pytest.mark.asyncio
async def test_manual_compact_replaces_effective_history_in_ordinary_session(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        graph = make_langgraph_execution(
            Config(workspace=str(tmp_path), model=ModelConfig(provider="anthropic", model="claude-3-5-sonnet")),
            api_key="test",
            session=session,
        )
        messages = []
        for role, cls, content in [
            ("user", HumanMessage, "old task " * 3000),
            ("assistant", AIMessage, "old progress " * 3000),
            ("user", HumanMessage, "latest request " * 3000),
        ]:
            row_id = await save_message(MessageRow(session_id=session.id, role=role, content=content))
            messages.append(cls(id=str(row_id), content=content))
        summary = AsyncMock(return_value="Preserved task and progress")
        old_persist = AsyncMock()
        adapter = GraphCompactionAdapter(
            graph._compaction_coordinator,
            run_compaction_agent=summary,
            persist_compaction=old_persist,
        )
        result = await adapter.compact(messages, messages, force=True, ask=False)
        assert result is not None
        rows = await load_messages(session.id)
        assert len(rows) == 1
        assert is_compaction_row(rows[0])
        assert rows[0].content == "Preserved task and progress"
        old_persist.assert_not_awaited()
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_legacy_runtime_resume_materializes_once_and_clears_summary(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        await save_runtime_state(session.id, RuntimeStateSnapshot(session_time="legacy time"))
        await execute_commit(
            "UPDATE session_runtime_state SET compaction_summary = ? WHERE session_id = ?",
            ("Legacy remembered work", session.id),
        )
        await save_message(MessageRow(session_id=session.id, role="user", content="remaining history"))
        host = SimpleNamespace(_session=session, _compaction_summary="stale")
        runtime = SessionRuntime(host)
        await runtime.restore_runtime_state()
        await runtime.restore_runtime_state()
        rows = await load_messages(session.id)
        assert len(rows) == 2
        assert is_compaction_row(rows[0])
        assert rows[0].additional_kwargs["compaction_depth"] == 0
        assert rows[0].content == "Legacy remembered work"
        assert rows[1].content == "remaining history"
        assert host._compaction_summary == ""
        assert host._session_date == "legacy time"
        assert await load_compaction_summary(session.id) == ""
    finally:
        await delete_session(session.id)


@pytest.mark.asyncio
async def test_runtime_persistence_does_not_write_new_summary(tmp_path):
    session = await create_session(workspace=str(tmp_path))
    try:
        host = SimpleNamespace(_session=session, _compaction_summary="obsolete", _session_date="today")
        await SessionRuntime(host).persist_runtime_state()
        assert await load_compaction_summary(session.id) == ""
        await save_runtime_state(session.id, RuntimeStateSnapshot(compaction_summary="obsolete direct write"))
        assert await load_compaction_summary(session.id) == ""
    finally:
        await delete_session(session.id)


@pytest.mark.parametrize(
    "force,ask,configured,hard,soft,preflight,approved,expected,asked",
    [
        (False, True, True, True, False, False, False, False, True),
        (False, True, True, True, False, False, True, True, True),
        (False, False, True, True, False, False, False, True, False),
        (True, True, True, False, False, False, False, True, False),
        (False, True, False, True, False, False, False, True, False),
        (False, True, True, False, False, False, True, False, False),
        (False, False, True, False, True, True, True, True, False),
    ],
)
async def test_manual_adapter_preserves_compaction_gates(
    force, ask, configured, hard, soft, preflight, approved, expected, asked,
):
    coordinator = SimpleNamespace(
        host=SimpleNamespace(
            config=SimpleNamespace(model=SimpleNamespace(model="test"), ask_compact=configured),
            _compaction=SimpleNamespace(
                is_overflow=lambda _: hard, is_soft_overflow=lambda _: soft,
            ),
        ),
        ask_compact=AsyncMock(return_value=approved),
        rollover_for_live_state=AsyncMock(return_value="replacement"),
    )
    result = await GraphCompactionAdapter(coordinator).compact(
        [HumanMessage(content="request")], force=force, ask=ask, preflight=preflight,
    )
    assert result == ("replacement" if expected else None)
    assert coordinator.rollover_for_live_state.await_count == int(expected)
    assert coordinator.ask_compact.await_count == int(asked)
