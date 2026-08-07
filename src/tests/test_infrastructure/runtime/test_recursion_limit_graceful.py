"""Graceful exit when the graph hits the recursion limit."""

from types import MethodType, SimpleNamespace

import pytest
from langgraph.errors import GraphRecursionError

from voidx.agent.adapters.persistence.session_repository import create_session, load_messages
from voidx.presentation.output.dock import BottomInputDock, set_dock
from voidx.agent.application.runtime_context import InteractionMode
from voidx.agent.domain.task.state import TaskState
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from voidx.agent.infrastructure.langgraph.runtime.compaction_coordinator import PreflightCompactionResult
from voidx.config import Config, ModelConfig


@pytest.mark.asyncio
async def test_recursion_limit_triggers_graceful_exit(tmp_path):
    session = await create_session(
        workspace=str(tmp_path),
        provider="mimo",
        model="mimo-v2.5",
    )
    execution = LangGraphExecution(Config(workspace=str(tmp_path)), api_key=None, session=session)
    execution.config = SimpleNamespace(
        workspace=str(tmp_path),
        model=ModelConfig(provider="mimo", model="mimo-v2.5", reasoning_effort="high"),
        agent=SimpleNamespace(recursion_limit=5),
    )
    execution._interaction_mode = InteractionMode.AUTO
    execution._task_state = TaskState()

    async def fake_maybe_compact(self, messages, session_messages, **_kwargs):
        return messages, None

    async def fake_preflight_compact(self, messages, session_msgs=None, **_kwargs):
        return None, PreflightCompactionResult(compacted=False)

    async def fake_astream(initial, _config, *, stream_mode="values"):
        yield {
            "messages": initial["messages"],
            "step_count": 2000,
            "task_state": initial.get("task_state"),
        }
        raise GraphRecursionError(
            "Recursion limit of 2000 reached without hitting a stop condition"
        )

    execution._maybe_compact = MethodType(fake_maybe_compact, execution)
    execution._preflight_compact_if_needed = MethodType(fake_preflight_compact, execution)
    execution.graph = SimpleNamespace(astream=fake_astream)
    execution._compaction = SimpleNamespace(prune=lambda _messages: None)

    test_dock = BottomInputDock()
    set_dock(test_dock)
    test_dock.begin_capture()
    try:
        # 达到 recursion limit 时不应向外抛异常，而是优雅收尾
        await execution.run_turn(
            "hello world",
            context=TurnExecutionContext(
                thread_id=execution.session_id or "coding",
                session_id=execution.session_id or "",
            ),
        )
    finally:
        test_dock.deactivate()
        test_dock.reset()
        set_dock(None)

    messages = await load_messages(session.id)
    roles = [m.role for m in messages]
    assert roles == ["user", "assistant"]
    assert "Step limit reached: 2000/2000" in messages[-1].content
    assert "Latest request" in messages[-1].content
