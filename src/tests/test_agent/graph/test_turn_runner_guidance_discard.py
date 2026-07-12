"""Tests for guidance discard in turn_runner finally block.

When a turn ends without an LLM call (e.g. early exception), pending guidance
should be discarded quietly, logged for diagnostics, and removed from the
active guidance preview.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from voidx.agent.graph.turn_runner import GraphTurnRunner
from voidx.llm.usage import UsageStats
from voidx.ui.output.events.schema import GuidanceCommitted, WarningAppended


class _RecordingEvents:
    def __init__(self) -> None:
        self.emitted: list = []

    async def emit(self, event) -> None:
        self.emitted.append(event)

    async def drain(self) -> None:
        pass

    def emit_direct(self, event) -> bool:
        self.emitted.append(event)
        return True

    async def request(self, event):
        return None


class _FailingSessionTracker:
    def begin_turn(self, workspace: str) -> None:
        raise RuntimeError("simulated early failure")

    def finish_turn(self) -> None:
        pass


def _make_host(*, via_events: bool = True) -> SimpleNamespace:
    events = _RecordingEvents()
    host = SimpleNamespace(
        _session=None,
        _workspace="/tmp/workspace",
        _pending_guidance=[("leftover guidance", False)],
        _usage_stats=UsageStats(),
        _ui=SimpleNamespace(
            events=events,
            via_events=lambda: via_events,
            session_tracker=_FailingSessionTracker(),
            dock=SimpleNamespace(
                append_message=lambda *a, **kw: None,
                clear_todo_state=lambda: None,
                set_input=lambda *a, **kw: None,
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
    return host


@pytest.mark.asyncio
async def test_finally_block_discards_pending_guidance_quietly_with_events(monkeypatch):
    host = _make_host(via_events=True)
    logged: list[dict] = []
    monkeypatch.setattr(
        "voidx.agent.graph.turn_runner.log_tool_event",
        lambda event, **kwargs: logged.append({"event": event, **kwargs}),
    )
    runner = GraphTurnRunner(host)

    with pytest.raises(RuntimeError, match="simulated early failure"):
        await runner.run_once("hello")

    warning_events = [e for e in host._ui.events.emitted if isinstance(e, WarningAppended)]
    committed_events = [e for e in host._ui.events.emitted if isinstance(e, GuidanceCommitted)]
    assert warning_events == []
    assert len(committed_events) == 1
    assert committed_events[0].source == "system"
    assert committed_events[0].text == ""
    assert host._pending_guidance == []
    assert logged == [{
        "event": "guidance_discarded",
        "message": "Guidance discarded: no LLM call to inject into.",
    }]


@pytest.mark.asyncio
async def test_finally_block_discards_pending_guidance_quietly_without_events_mode(monkeypatch):
    host = _make_host(via_events=False)
    dock_messages: list[str] = []
    clear_calls: list[bool] = []
    host._ui.dock.append_message = lambda text, **kw: dock_messages.append(text)
    host._ui.dock.clear_guidance_preview = lambda: clear_calls.append(True)
    logged: list[dict] = []
    monkeypatch.setattr(
        "voidx.agent.graph.turn_runner.log_tool_event",
        lambda event, **kwargs: logged.append({"event": event, **kwargs}),
    )
    runner = GraphTurnRunner(host)

    with pytest.raises(RuntimeError, match="simulated early failure"):
        await runner.run_once("hello")

    assert dock_messages == []
    assert clear_calls == [True]
    assert host._pending_guidance == []
    assert logged == [{
        "event": "guidance_discarded",
        "message": "Guidance discarded: no LLM call to inject into.",
    }]


@pytest.mark.asyncio
async def test_finally_block_no_warning_when_no_pending_guidance():
    host = _make_host(via_events=True)
    host._pending_guidance = []
    runner = GraphTurnRunner(host)

    with pytest.raises(RuntimeError, match="simulated early failure"):
        await runner.run_once("hello")

    warning_events = [e for e in host._ui.events.emitted if isinstance(e, WarningAppended)]
    assert len(warning_events) == 0
