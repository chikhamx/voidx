"""Tests for user-submitted guidance rendering events."""

from types import SimpleNamespace

from voidx.agent.infrastructure.langgraph.execution import LangGraphExecution
from voidx.agent.infrastructure.langgraph.execution import GUIDANCE_MAX_CHARS
from voidx.presentation.output.events.schema import GuidanceSubmitted


class _Events:
    def __init__(self, *, succeeds: bool = True) -> None:
        self.succeeds = succeeds
        self.emitted = []

    def emit_direct(self, event) -> bool:
        self.emitted.append(event)
        return self.succeeds


def _graph(*, succeeds: bool = True) -> tuple[LangGraphExecution, _Events]:
    graph = object.__new__(LangGraphExecution)
    events = _Events(succeeds=succeeds)
    graph._pending_guidance = []
    graph._ui = SimpleNamespace(events=events, via_events=lambda: True)
    return graph, events


def test_submit_user_guidance_emits_guidance_submitted_only():
    graph, events = _graph()

    assert graph.submit_guidance("  use   TypeScript  ", source="user") is True

    assert graph._pending_guidance == [("use TypeScript", False, "user")]
    assert events.emitted == [
        GuidanceSubmitted(text="use TypeScript", truncated=False),
    ]


def test_submit_user_guidance_marks_truncated_display_without_polluting_llm_input():
    graph, events = _graph()

    assert graph.submit_guidance("x" * (GUIDANCE_MAX_CHARS + 1), source="user") is True

    guidance = "x" * GUIDANCE_MAX_CHARS
    assert graph._pending_guidance == [(guidance, True, "user")]
    assert events.emitted == [
        GuidanceSubmitted(text=guidance, truncated=True),
    ]


def test_submit_user_guidance_fails_without_queueing_when_emit_fails():
    graph, events = _graph(succeeds=False)

    assert graph.submit_guidance("keep going", source="user") is False

    assert graph._pending_guidance == []
    assert events.emitted == [GuidanceSubmitted(text="keep going", truncated=False)]


def test_submit_guidance_rejects_blank_text_without_events():
    graph, events = _graph()

    assert graph.submit_guidance("   ", source="user") is False

    assert graph._pending_guidance == []
    assert events.emitted == []


def test_drain_pending_guidance_returns_human_messages_with_truncated_flags():
    graph, _ = _graph()

    graph.submit_guidance("use TypeScript", source="user")
    graph.submit_guidance("x" * (GUIDANCE_MAX_CHARS + 1), source="user")

    drained = graph._drain_pending_guidance()

    assert len(drained) == 2
    msg1, trunc1, source1 = drained[0]
    msg2, trunc2, source2 = drained[1]
    assert msg1.content == "use TypeScript"
    assert trunc1 is False
    assert source1 == "user"
    assert msg2.content == "x" * GUIDANCE_MAX_CHARS
    assert trunc2 is True
    assert source2 == "user"
    assert all(
        msg.additional_kwargs.get("_voidx_guidance") is True
        for msg, _, _ in drained
    )
    assert graph._pending_guidance == []


def test_drain_pending_guidance_empty_when_no_guidance():
    graph, _ = _graph()

    drained = graph._drain_pending_guidance()

    assert drained == []


def test_drain_pending_guidance_preserves_fifo_order():
    graph, _ = _graph()

    graph.submit_guidance("first", source="user")
    graph.submit_guidance("second", source="user")
    graph.submit_guidance("third", source="user")

    drained = graph._drain_pending_guidance()

    assert [msg.content for msg, _, _ in drained] == ["first", "second", "third"]
