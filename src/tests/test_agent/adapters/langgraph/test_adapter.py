from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from langchain_core.messages import ToolMessage
from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.turn.state import TurnPhase
from voidx.agent.adapters.langgraph.adapter import LangGraphTurnEngine, _evidence_from_execution
from voidx.agent.adapters.langgraph.state_mapper import LangGraphStateMapper
from voidx.agent.domain.task.state import GoalSpec, TaskState
from voidx.agent.domain.task.intent import InteractionMode


@dataclass
class FakeHost:
    _interaction_mode: InteractionMode = InteractionMode.AUTO
    _task_state: TaskState = field(default_factory=TaskState)
    _compaction_summary: str = ""
    _session_date: str = ""
    calls: list[tuple[str, str | None, bool]] = field(default_factory=list)

    @property
    def session_id(self):
        return ""

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

    async def run_turn(self, user_text, *, display_text=None, context=None, persist_user_input=True):
        self.calls.append((user_text, display_text, persist_user_input))


@pytest.mark.asyncio
async def test_turn_engine_maps_runtime_and_delegates_to_execution():
    host = FakeHost()
    engine = LangGraphTurnEngine(host)
    runtime = SessionRuntimeState(
        interaction_mode=InteractionMode.PLAN,
        compaction_summary="before",
        session_time="2026-07-19 CST",
        turn_phase=TurnPhase.RUNNING,
    )

    result = await engine.run("continue", runtime, display_text="stay focused")

    assert host.calls == [("continue", "stay focused", True)]
    assert host._interaction_mode is InteractionMode.PLAN
    assert host._compaction_summary == "before"
    assert result.interaction_mode is InteractionMode.PLAN
    # The engine returns the post-execution state still RUNNING; the runtime
    # facade owns the COMMITTED transition via advance_turn.
    assert result.turn_phase is TurnPhase.RUNNING


def test_state_mapper_is_the_runtime_graph_host_conversion_boundary():
    host = FakeHost()
    mapper = LangGraphStateMapper()
    runtime = SessionRuntimeState(
        interaction_mode=InteractionMode.GOAL,
        task_state=TaskState(current_goal=GoalSpec(desc="ship adapter")),
        compaction_summary="summary",
        session_time="2026-07-19 CST",
        turn_phase=TurnPhase.RUNNING,
    )

    mapper.apply_runtime(host, runtime)
    restored = mapper.runtime_from_execution(host, turn_phase=TurnPhase.COMMITTED)

    assert restored == runtime.model_copy(update={"turn_phase": TurnPhase.COMMITTED})
    assert restored.task_state is not runtime.task_state


def test_evidence_from_execution_keeps_large_tool_batch_counts():
    host = FakeHost()
    host._current_messages = [
        ToolMessage(
            content="{'ok': True, 'recipient_name': 'IM集团'}",
            tool_call_id=f"call-{index}",
            name="typex.send_message",
        )
        for index in range(100)
    ]

    evidence = _evidence_from_execution(host)

    summaries = "\n".join(evidence["tool_result_summaries"])
    assert "Observed tool result total: 100" in summaries
    assert "typex.send_message=100" in summaries
    assert "ok_true=100" in summaries
    assert "omitted_middle_tool_results=60" in summaries

