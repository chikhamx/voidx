"""Tests for LLM retry sleep behavior."""

import pytest

from voidx.agent.adapters.langgraph.runtime.core import loop as loop_module
from voidx.agent.adapters.langgraph.runtime.core.helpers import LLMErrorKind


class _SilentUi:
    class _Printer:
        def print(self, *_args, **_kwargs) -> None:
            pass

    ui = _Printer()

    def via_events(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_llm_retry_sleep_uses_agent_test_simulated_delay(monkeypatch):
    observed_delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        observed_delays.append(delay)

    monkeypatch.setattr(loop_module.asyncio, "sleep", record_sleep)

    result = await loop_module.handle_llm_exception(
        ui=_SilentUi(),
        loop=loop_module.LlmLoopState(context_tokens=0),
        error=ConnectionError("boom"),
        kind=LLMErrorKind.NETWORK,
        max_retries=1,
        timeout_max_retries=1,
    )

    assert result.action == "retry"
    assert observed_delays == [0.002]
