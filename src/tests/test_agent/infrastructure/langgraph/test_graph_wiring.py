from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from voidx.agent.domain.state import AgentRuntime
from voidx.agent.domain.turn import TurnPhase
from voidx.agent.infrastructure.langgraph.adapter import LangGraphTurnEngine
from voidx.runtime import InteractionMode, TaskState


@dataclass
class FakeBackend:
    _interaction_mode: InteractionMode = InteractionMode.AUTO
    _task_state: TaskState = field(default_factory=TaskState)
    _compaction_summary: str = ""
    _session_date: str = ""
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    @property
    def interaction_mode(self):
        return self._interaction_mode

    @property
    def task_state(self):
        return self._task_state

    @property
    def compaction_summary(self):
        return self._compaction_summary

    @property
    def session_date(self):
        return self._session_date

    def set_interaction_mode(self, value):
        self._interaction_mode = value

    def set_task_state(self, value):
        self._task_state = value

    def set_compaction_summary(self, value):
        self._compaction_summary = value

    def set_session_date(self, value):
        self._session_date = value

    async def run_turn(
        self,
        user_text: str,
        *,
        display_text: str | None = None,
        context=None,
    ) -> None:
        self.calls.append((user_text, display_text))


@pytest.mark.asyncio
async def test_langgraph_engine_delegates_turn_with_runtime_boundary():
    backend = FakeBackend()
    engine = LangGraphTurnEngine(backend)
    runtime = AgentRuntime(turn_phase=TurnPhase.RUNNING)

    result = await engine.run("hello", runtime)

    assert backend.calls == [("hello", None)]
    assert result.turn_phase is TurnPhase.COMMITTED
