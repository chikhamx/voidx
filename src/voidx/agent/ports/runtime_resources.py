"""Capabilities supplied to an AgentRuntime."""

from __future__ import annotations

from typing import Protocol

from voidx.agent.ports.events import EventPublisher
from voidx.agent.ports.session import SessionStore
from voidx.agent.ports.turn_engine import TurnEngine


class RuntimeResources(Protocol):
    """Runtime dependencies; mutable thread state remains outside this port."""

    @property
    def turn_engine(self) -> TurnEngine: ...

    @property
    def sessions(self) -> SessionStore: ...

    @property
    def events(self) -> EventPublisher: ...
