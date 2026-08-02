"""Shared fixtures for test_infrastructure tests."""

import pytest


@pytest.fixture(autouse=True)
def simulated_llm_retry_sleep(monkeypatch: pytest.MonkeyPatch):
    def simulated_delay(_delay: float) -> float:
        return 0.002

    monkeypatch.setattr(
        "voidx.agent.infrastructure.langgraph.runtime.core.loop._llm_retry_sleep_delay",
        simulated_delay,
    )
    monkeypatch.setattr(
        "voidx.agent.infrastructure.langgraph.runtime.subagent._llm_retry_sleep_delay",
        simulated_delay,
    )
