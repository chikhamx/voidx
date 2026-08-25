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
