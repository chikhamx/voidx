"""Application service for isolated Chat turns."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from voidx.agent.application.runtime.contracts import TurnRequest, TurnResult
from voidx.agent.domain.chat_policy import ChatResourceScope, ChatToolView
from voidx.agent.domain.profile import CHAT_PROFILE
from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.domain.thread import AgentThread



class CreatedSession(Protocol):
    id: str


class SessionCreator(Protocol):
    async def __call__(
        self,
        *,
        workspace: str,
        directory: str,
        profile: str,
    ) -> CreatedSession: ...


class ChatService:
    """Build chat-scoped turns and delegate execution to the AgentRuntime.

    The service owns chat thread identity, profile selection and the fixed tool
    view. It never executes tools, touches LangGraph, or persists runtime state;
    those stay with the runtime facade and the LangGraph infrastructure.
    """

    def __init__(self, runtime, *, session_creator: SessionCreator) -> None:
        self._runtime = runtime
        self._session_creator = session_creator

    async def run_chat_turn(self, **kwargs):
        return await self.run_turn(**kwargs)

    async def run_turn(
        self,
        *,
        user_text: str,
        thread: AgentThread | None = None,
        runtime_state: SessionRuntimeState | None = None,
        workspace: str | Path | None = None,
        display_text: str | None = None,
        context: TurnExecutionContext | None = None,
    ) -> TurnResult:
        if thread is None:
            session = await self._create_session(workspace)
            thread = AgentThread(thread_id=f"chat:{session.id}", session_id=session.id)
        elif not thread.thread_id.startswith("chat:"):
            raise ValueError("Chat thread_id must use the chat: prefix")

        scope = ChatResourceScope(workspace=workspace)
        tool_view = ChatToolView.for_scope(scope)
        expected_context = TurnExecutionContext(
            thread_id=thread.thread_id,
            session_id=thread.session_id or "",
            runtime_profile=CHAT_PROFILE,
            workspace=str(workspace or ""),
            tool_policy=tool_view,
        )
        if context is not None and context != expected_context:
            raise ValueError("Chat turn context does not match thread, workspace, profile, or tool policy")
        execution_context = expected_context
        return await self._runtime.run_turn(
            TurnRequest(
                thread=thread,
                user_text=user_text,
                # Chat always supplies its input snapshot explicitly; the runtime
                # treats the caller-provided state as authoritative.
                runtime=runtime_state or SessionRuntimeState(),
                display_text=display_text,
                context=execution_context,
            )
        )

    async def _create_session(self, workspace: str | Path | None) -> CreatedSession:
        normalized = str(Path(workspace).expanduser().resolve()) if workspace is not None else ""
        return await self._session_creator(
            workspace=normalized,
            directory=normalized,
            profile="chat",
        )
