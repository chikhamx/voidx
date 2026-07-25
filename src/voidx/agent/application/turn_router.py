"""Profile-aware construction of runtime turn requests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from voidx.agent.application.chat_service import CHAT_PROFILE
from voidx.agent.application.coding_service import CODING_PROFILE
from voidx.agent.domain.chat_policy import ChatResourceScope, ChatToolView
from voidx.agent.domain.thread import AgentThread
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.runtime.contracts import TurnRequest
from voidx.memory.service import get_session


class TurnRouter:
    def __init__(self, *, default_workspace: str = "") -> None:
        self._default_workspace = default_workspace

    async def build_request(
        self,
        user_text: str,
        *,
        thread_id: str = "",
        context: Any | None = None,
        workspace: str | Path | None = None,
    ) -> TurnRequest:
        effective_thread_id = thread_id or str(getattr(context, "thread_id", "") or "")
        session_id = str(getattr(context, "session_id", "") or effective_thread_id)
        session = await get_session(session_id) if session_id else None
        profile_id = getattr(session, "runtime_profile", "coding") or "coding"
        if profile_id == "chat":
            workspace_value = str(
                workspace or getattr(session, "workspace", "") or getattr(session, "directory", "") or self._default_workspace
            )
            profile = CHAT_PROFILE
            policy = ChatToolView.for_scope(ChatResourceScope(workspace=workspace_value or None))
            thread = AgentThread(thread_id=effective_thread_id if effective_thread_id.startswith("chat:") else f"chat:{session_id}", session_id=session_id or None)
        else:
            workspace_value = str(workspace or self._default_workspace)
            profile = CODING_PROFILE
            policy = None
            thread = AgentThread(thread_id=effective_thread_id or session_id or "coding", session_id=session_id or None)
        turn_context = TurnExecutionContext(
            thread_id=thread.thread_id,
            session_id=thread.session_id or "",
            runtime_profile=profile,
            workspace=workspace_value,
            tool_policy=policy,
        )
        return TurnRequest(thread=thread, user_text=user_text, context=turn_context)
