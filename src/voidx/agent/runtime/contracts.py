"""Contracts at the reusable runtime boundary."""

from __future__ import annotations

from voidx.agent.domain.turn_context import TurnExecutionContext

from pydantic import BaseModel, ConfigDict

from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.thread import AgentThread, LifecycleState


class TurnRequest(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    thread: AgentThread
    user_text: str
    context: TurnExecutionContext
    display_text: str | None = None
    # ``None`` means the caller did not supply an input snapshot; the runtime
    # then loads the persisted state for ``thread.session_id``. Supplying a
    # state (even a default-constructed one) makes the caller's snapshot
    # authoritative and the runtime will not reload it from the store.
    runtime: SessionRuntimeState | None = None


class TurnResult(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    thread: AgentThread
    lifecycle: LifecycleState
    runtime: SessionRuntimeState | None = None
    error: str | None = None

    @property
    def session_id(self) -> str | None:
        return self.thread.session_id
