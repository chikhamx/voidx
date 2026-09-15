"""Semantic event publishing ports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from voidx.agent.domain.events import AgentEvent

if TYPE_CHECKING:
    from voidx.agent.domain.semantic_events import SemanticEvent


class EventPublisher(Protocol):
    def publish(self, event: AgentEvent) -> None: ...


class SemanticEventPublisher(Protocol):
    async def publish(self, event: SemanticEvent) -> None: ...


class NullSemanticEventPublisher:
    """Discard output immediately for explicitly unobserved internal runs."""

    async def publish(self, event: SemanticEvent) -> None:
        return None
