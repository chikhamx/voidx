from __future__ import annotations

from types import SimpleNamespace

import pytest

from voidx.agent.adapters.langgraph.runtime.turn_runner import TurnRunner
from voidx.agent.adapters.langgraph.runtime.thread_context import current_thread_execution_state
from voidx.agent.domain.guidance import Guidance
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.llm.usage import UsageStats


class _FailingSessionTracker:
    def begin_turn(self, workspace: str) -> None:
        raise RuntimeError("simulated early failure")

    def finish_turn(self) -> None:
        pass


class _RecordingGuidance:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str | None]] = []
        self.released: list[str] = []
        self.committed: list[str] = []

    async def bind_delivery(
        self,
        delivery_id: str,
        *,
        session_id: str = "",
        thread_id: str = "",
        run_id: str = "",
        phase: str | None = None,
    ) -> list[Guidance]:
        self.calls.append((delivery_id, session_id, thread_id, phase))
        return [
            Guidance(
                guidance_id="guidance-turn-1",
                text="keep the turn focused",
                target_thread_id=thread_id,
            )
        ]

    async def release_delivery(self, delivery_id: str) -> None:
        self.released.append(delivery_id)

    async def commit_delivery(self, delivery_id: str) -> None:
        self.committed.append(delivery_id)


def _make_host(guidance: _RecordingGuidance) -> SimpleNamespace:
    return SimpleNamespace(
        _session=None,
        _workspace="/tmp/workspace",
        _guidance_service=guidance,
        _pending_guidance=[],
        _usage_stats=UsageStats(),
        _ui=SimpleNamespace(
            via_events=lambda: False,
            session_tracker=_FailingSessionTracker(),
            dock=SimpleNamespace(
                append_message=lambda *args, **kwargs: None,
                clear_todo_state=lambda: None,
                set_input=lambda *args, **kwargs: None,
            ),
        ),
        _thread_execution_states={},
        _session_msg_cache=None,
        _context_cache=None,
        _interaction_mode=None,
        _task_state=None,
        _compaction_summary="",
        _pending_summary=None,
        _session_date="",
        _runtime_guards=None,
    )


@pytest.mark.asyncio
async def test_turn_runner_binds_and_releases_durable_guidance_before_llm_failure() -> None:
    guidance = _RecordingGuidance()
    runner = TurnRunner(_make_host(guidance))
    context = TurnExecutionContext(
        thread_id="thread-1",
        session_id="session-1",
        runtime_profile=RuntimeProfile(
            profile_id="coding", revision=1, name="Coding"
        ),
    )

    with pytest.raises(RuntimeError, match="simulated early failure"):
        await runner.run_once("hello", context=context)

    assert len(guidance.calls) == 1
    delivery_id, session_id, thread_id, phase = guidance.calls[0]
    assert delivery_id.startswith("turn:")
    assert session_id == "session-1"
    assert thread_id == "thread-1"
    assert phase == "work"
    assert guidance.released == [delivery_id]
    assert guidance.committed == []


class _SuccessfulSessionTracker:
    def begin_turn(self, workspace: str) -> None:
        pass

    def finish_turn(self) -> None:
        pass

    def change_summary_lines(self) -> list[str]:
        return []


async def _no_preflight(*_args, **_kwargs):
    return None, None


async def _no_transcript_snapshot() -> None:
    pass


class _SuccessfulGraph:
    def __init__(self, host: SimpleNamespace) -> None:
        self.host = host

    async def astream(self, initial, *_args, **_kwargs):
        state = current_thread_execution_state()
        if state is not None:
            state.pending_guidance.clear()
        self.host._pending_guidance.clear()
        yield {
            "messages": [*initial["messages"]],
            "task_state": initial["task_state"],
        }


def _make_successful_host(guidance: _RecordingGuidance) -> SimpleNamespace:
    host = SimpleNamespace(
        _session=None,
        _workspace="/tmp/workspace",
        _guidance_service=guidance,
        _pending_guidance=[],
        _usage_stats=UsageStats(),
        _ui=SimpleNamespace(
            via_events=lambda: False,
            session_tracker=_SuccessfulSessionTracker(),
            dock=SimpleNamespace(
                tree=None,
                append_message=lambda *args, **kwargs: None,
                clear_todo_state=lambda: None,
                set_input=lambda *args, **kwargs: None,
                start_turn=lambda *args, **kwargs: None,
                commit_todo_state=lambda: None,
            ),
        ),
        _thread_execution_states={},
        _session_msg_cache=None,
        _context_cache=None,
        _interaction_mode=None,
        _task_state=None,
        _compaction_summary="",
        _pending_summary=None,
        _session_date="",
        _runtime_guards=None,
        _pending_turn_stop_commit=None,
        _any_messages_sent=False,
        _plan_mode=False,
        model=None,
        _compaction=SimpleNamespace(prune=lambda messages: None),
        _persist_transcript_snapshot=_no_transcript_snapshot,
        _preflight_compact_if_needed=_no_preflight,
        _resolve_skill_references=lambda text: SimpleNamespace(prefix="", remove_spans=[]),
        _mcp_reference_resolver=None,
        _settings=None,
        mcp_manager=None,
    )
    host.graph = _SuccessfulGraph(host)
    return host


@pytest.mark.asyncio
async def test_turn_runner_commits_durable_guidance_after_successful_consumption() -> None:
    guidance = _RecordingGuidance()
    host = _make_successful_host(guidance)
    runner = TurnRunner(host)
    context = TurnExecutionContext(
        thread_id="thread-1",
        session_id="session-1",
        runtime_profile=RuntimeProfile(
            profile_id="coding", revision=1, name="Coding"
        ),
        detached=True,
    )

    await runner.run_once("hello", context=context, persist_user_input=False)

    assert len(guidance.calls) == 1
    delivery_id = guidance.calls[0][0]
    assert guidance.committed == [delivery_id]
    assert guidance.released == []
