"""Tests for hidden runtime guard guidance."""

from types import SimpleNamespace

from voidx.agent.adapters.langgraph.execution import LangGraphExecution
from voidx.agent.adapters.langgraph.runtime.runtime_guards import GuardGuidance
from voidx.agent.adapters.langgraph.runtime.tool_executor.guards import _submit_guard_guidance
from voidx.presentation.output.events.schema import GuidanceSubmitted


class _Events:
    def __init__(self, *, succeeds: bool = True) -> None:
        self.succeeds = succeeds
        self.emitted = []

    def emit_direct(self, event) -> bool:
        self.emitted.append(event)
        return self.succeeds


def test_submit_guard_guidance_stays_hidden_and_queues_when_event_bus_rejects():
    graph = object.__new__(LangGraphExecution)
    events = _Events(succeeds=False)
    graph._pending_guidance = []
    graph._ui = SimpleNamespace(events=events, via_events=lambda: True)

    assert graph.submit_guidance("retry differently", source="guard") is True

    assert graph._pending_guidance == [("retry differently", False, "guard")]
    assert events.emitted == []


def test_drain_guard_guidance_marks_message_without_displaying_it():
    graph = object.__new__(LangGraphExecution)
    events = _Events()
    graph._pending_guidance = []
    graph._ui = SimpleNamespace(events=events, via_events=lambda: True)

    assert graph.submit_guidance("retry differently", source="guard") is True

    drained = graph._drain_pending_guidance()

    assert len(drained) == 1
    msg, truncated, source = drained[0]
    assert msg.content == "retry differently"
    assert truncated is False
    assert source == "guard"
    assert events.emitted == []


def test_runtime_guard_submits_guidance_with_guard_source():
    calls = []
    host = SimpleNamespace(
        submit_guidance=lambda text, **kwargs: calls.append((text, kwargs)) or True,
    )
    guidance = GuardGuidance(kind="loop", level="light", message="change approach")

    _submit_guard_guidance(host, guidance)

    assert calls == [("change approach", {"source": "guard"})]


def test_runtime_guard_fallback_queues_hidden_guard_source():
    pending = []
    host = SimpleNamespace(_pending_guidance=pending)
    guidance = GuardGuidance(kind="loop", level="light", message="change approach")

    _submit_guard_guidance(host, guidance)

    assert pending == [("change approach", False, "guard")]
