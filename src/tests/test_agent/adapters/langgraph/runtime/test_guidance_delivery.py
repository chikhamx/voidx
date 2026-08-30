from __future__ import annotations

import pytest

from tests.langgraph_execution import make_langgraph_execution
from voidx.agent.adapters.persistence.thread_repository import ThreadStore
from voidx.agent.application.guidance_service import GuidanceService
from voidx.config import Config
from voidx.llm.domain.model import ModelConfig


@pytest.mark.asyncio
async def test_execution_submits_durable_guidance_before_memory_wakeup(tmp_path) -> None:
    execution = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    store = ThreadStore(tmp_path / "guidance.db")
    execution.bind_guidance_service(
        GuidanceService(store, id_factory=lambda: "guidance-fixed")
    )

    assert execution.submit_guidance(
        "keep the API compatible",
        source="user",
        thread_id="thread-1",
        session_id="session-1",
    ) is True

    persisted = await store.get_guidance("guidance-fixed")
    assert persisted is not None
    assert persisted.text == "keep the API compatible"
    assert persisted.target_thread_id == "thread-1"
    assert persisted.target_session_id == "session-1"
    assert persisted.delivery_id is None
    assert len(execution._pending_guidance) == 1
    assert execution._pending_guidance[0].guidance_id == persisted.guidance_id


@pytest.mark.asyncio
async def test_mid_turn_guidance_is_consumed_by_active_delivery_without_redelivery(tmp_path) -> None:
    execution = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    store = ThreadStore(tmp_path / "guidance.db")
    guidance_service = GuidanceService(
        store,
        id_factory=lambda: "guidance-mid-turn",
    )
    execution.bind_guidance_service(guidance_service)

    from voidx.agent.adapters.langgraph.runtime.thread_context import (
        bind_thread_execution_context,
        current_thread_execution_state,
    )
    from voidx.agent.domain.turn_context import TurnExecutionContext

    async with bind_thread_execution_context(
        execution,
        thread_id="thread-1",
        turn_context=TurnExecutionContext(thread_id="thread-1", session_id=""),
    ):
        state = current_thread_execution_state()
        assert state is not None
        state.guidance_delivery_id = "turn-1"
        submitted = guidance_service.submit_guidance(
            "keep the API compatible",
            source="user",
            thread_id="thread-1",
        )
        assert submitted is not None
        assert submitted.delivery_id == "turn-1"

    await guidance_service.commit_delivery("turn-1")

    assert await guidance_service.bind_delivery(
        "turn-2",
        thread_id="thread-1",
    ) == []


@pytest.mark.asyncio
async def test_rebinding_idle_guidance_replaces_memory_entry_without_duplicate(tmp_path) -> None:
    execution = make_langgraph_execution(
        Config(
            model=ModelConfig(provider="mimo", model="mimo-v2.5"),
            workspace=str(tmp_path),
        ),
        api_key="test-key",
    )
    store = ThreadStore(tmp_path / "guidance.db")
    guidance_service = GuidanceService(
        store,
        id_factory=lambda: "guidance-idle",
    )
    execution.bind_guidance_service(guidance_service)
    assert execution.submit_guidance("keep the API compatible", thread_id="thread-1") is True

    from voidx.agent.adapters.langgraph.runtime.thread_context import (
        bind_thread_execution_context,
        current_thread_execution_state,
    )
    from voidx.agent.adapters.langgraph.runtime.turn_runner import _project_guidance_snapshots
    from voidx.agent.domain.turn_context import TurnExecutionContext

    context = TurnExecutionContext(thread_id="thread-1", session_id="")
    async with bind_thread_execution_context(
        execution,
        thread_id="thread-1",
        turn_context=context,
    ):
        state = current_thread_execution_state()
        assert state is not None
        state.guidance_delivery_id = "turn-1"
        bound = await guidance_service.bind_delivery("turn-1", thread_id="thread-1")
        _project_guidance_snapshots(execution, bound, context)

        drained = execution._drain_pending_guidance()

    assert [message.content for message, _, _ in drained] == ["keep the API compatible"]
    assert await guidance_service.commit_guidance_ids({"guidance-idle"}) is None
    assert await guidance_service.bind_delivery("turn-2", thread_id="thread-1") == []
