from voidx.agent.domain.events import AgentEvent, AgentEventKind


def test_agent_event_is_presentation_agnostic() -> None:
    event = AgentEvent(
        kind=AgentEventKind.TURN_STARTED,
        message="turn started",
        metadata={"thread_id": "thread-1"},
    )

    assert event.kind is AgentEventKind.TURN_STARTED
    assert event.metadata == {"thread_id": "thread-1"}
