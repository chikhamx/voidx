"""Application service for isolated Chat turns."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from voidx.agent.domain.chat_policy import ChatResourceScope, ChatToolView
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.thread import AgentThread
from voidx.agent.runtime.contracts import TurnRequest, TurnResult
from voidx.memory.service import SessionInfo, create_session
from voidx.runtime.ui import ThreadExecutionContext


CHAT_PROFILE = RuntimeProfile(profile_id="chat", revision=1, name="Chat")


class ChatService:
    """Build chat-scoped turns and delegate execution to the AgentRuntime.

    The service owns chat thread identity, profile selection and the fixed tool
    view. It never executes tools, touches LangGraph, or persists runtime state;
    those stay with the runtime facade and the LangGraph infrastructure.
    """

    def __init__(self, runtime) -> None:
        self._runtime = runtime

    async def run_turn(
        self,
        *,
        user_text: str,
        thread: AgentThread | None = None,
        runtime_state: SessionRuntimeState | None = None,
        workspace: str | Path | None = None,
        display_text: str | None = None,
        context: Any | None = None,
    ) -> TurnResult:
        if thread is None:
            session = await self._create_session(workspace)
            thread = AgentThread(thread_id=f"chat:{session.id}", session_id=session.id)
        elif not thread.thread_id.startswith("chat:"):
            raise ValueError("Chat thread_id must use the chat: prefix")

        scope = ChatResourceScope(workspace=workspace)
        tool_view = ChatToolView.for_scope(scope)
        execution_context = context or ThreadExecutionContext(
            thread_id=thread.thread_id,
            session_id=thread.session_id or "",
            tool_view=tool_view,
        )
        return await self._runtime.run_turn(
            TurnRequest(
                thread=thread,
                user_text=user_text,
                profile=CHAT_PROFILE,
                # Chat always supplies its input snapshot explicitly; the runtime
                # treats the caller-provided state as authoritative.
                runtime=runtime_state or SessionRuntimeState(),
                display_text=display_text,
                context=execution_context,
            )
        )

    @staticmethod
    async def _create_session(workspace: str | Path | None) -> SessionInfo:
        normalized = str(Path(workspace).expanduser().resolve()) if workspace is not None else ""
        return await create_session(
            workspace=normalized,
            directory=normalized,
            profile="chat",
        )
