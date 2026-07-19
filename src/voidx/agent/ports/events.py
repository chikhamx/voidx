"""Semantic event publishing port."""

from typing import Protocol

from voidx.agent.domain.events import AgentEvent


class EventPublisher(Protocol):
    def publish(self, event: AgentEvent) -> None: ...
