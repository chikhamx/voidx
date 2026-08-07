"""Narrow ports between agent use cases and presentation adapters."""

from __future__ import annotations

from typing import Any, Protocol

from voidx.agent.domain.turn_context import TurnExecutionContext


class TurnRunner(Protocol):
    async def run_turn(
        self,
        user_text: str,
        *,
        thread_id: str = "",
        context: TurnExecutionContext | None = None,
        display_text: str | None = None,
    ) -> None: ...


class SessionLifecycle(Protocol):
    async def restore_runtime_state(self) -> None: ...
    async def delete_empty_current_session(self) -> None: ...
    async def clear_current_session(self) -> None: ...


class RuntimeStatusReader(Protocol):
    def runtime_status(self) -> Any: ...


class GuidancePort(Protocol):
    def can_submit_guidance(self) -> bool: ...
    def submit_guidance(self, text: str, **kwargs: Any) -> bool: ...


class InteractiveInputPort(Protocol):
    async def dispatch_input(
        self,
        user_input: str,
        *,
        context: TurnExecutionContext | None = None,
        thread_id: str = "",
    ) -> bool: ...


class AgentEventPublisher(Protocol):
    def publish_message(self, message: str) -> None: ...
    def start_turn(self, text: str) -> None: ...


class PresentationSnapshotPort(Protocol):
    async def persist_current(self, session_id: str) -> None: ...
    async def restore_current(self, session_id: str, *, append: bool = False) -> bool: ...
    async def clear(self, session_id: str) -> None: ...


class NullPresentationSnapshotPort:
    async def persist_current(self, session_id: str) -> None:
        return None

    async def restore_current(self, session_id: str, *, append: bool = False) -> bool:
        return False

    async def clear(self, session_id: str) -> None:
        return None


class NullAgentEventPublisher:
    def publish_message(self, message: str) -> None:
        return None

    def start_turn(self, text: str) -> None:
        return None
