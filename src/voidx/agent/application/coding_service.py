"""Application service for default Coding turns."""

from __future__ import annotations

from typing import Any

from voidx.agent.domain.prompt_policy import CodingPromptPolicy
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.thread import AgentThread
from voidx.agent.runtime.contracts import TurnRequest, TurnResult


CODING_PROFILE = RuntimeProfile(
    profile_id="coding", revision=1, name="Coding", prompt_policy=CodingPromptPolicy()
)


class CodingService:
    """Build coding-scoped turns and delegate execution to the AgentRuntime.

    The service owns coding thread identity, profile selection and TurnRequest
    construction. UI, slash command handling, and runtime persistence remain in
    the interactive service and runtime facade.
    """

    def __init__(self, runtime) -> None:
        self._runtime = runtime

    async def run_turn(
        self,
        *,
        user_text: str,
        thread_id: str = "",
        session_id: str | None = None,
        context: Any | None = None,
    ) -> TurnResult:
        resolved_thread_id = thread_id or session_id or "coding"
        thread = AgentThread(
            thread_id=resolved_thread_id,
            session_id=session_id or None,
        )
        return await self._runtime.run_turn(
            TurnRequest(
                thread=thread,
                user_text=user_text,
                profile=CODING_PROFILE,
                runtime=None,
                context=context,
            )
        )
