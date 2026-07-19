from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from voidx.agent.domain.state import AgentRuntime
from voidx.agent.domain.turn import TurnPhase
from voidx.agent.infrastructure.langgraph.adapter import LangGraphTurnEngine
from voidx.runtime import InteractionMode, TaskState


@dataclass
class FakeRunner:
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    async def run_once(
        self,
        user_text: str,
        *,
        display_text: str | None = None,
        context=None,
    ) -> None:
        self.calls.append((user_text, display_text))


@dataclass
class FakeBackend:
    _interaction_mode: InteractionMode = InteractionMode.AUTO
    _task_state: TaskState = field(default_factory=TaskState)
    _compaction_summary: str = ""
    _session_date: str = ""


@pytest.mark.asyncio
async def test_langgraph_engine_delegates_turn_with_runtime_boundary():
    backend = FakeBackend()
    runner = FakeRunner()
    engine = LangGraphTurnEngine(backend, runner=runner)
    runtime = AgentRuntime(turn_phase=TurnPhase.RUNNING)

    result = await engine.run("hello", runtime)

    assert runner.calls == [("hello", None)]
    assert result.turn_phase is TurnPhase.COMMITTED
