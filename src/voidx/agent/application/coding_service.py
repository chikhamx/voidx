"""Application service for default Coding turns."""

from __future__ import annotations

from voidx.agent.domain.prompt_policy import CodingPromptPolicy
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.turn_context import TurnExecutionContext
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
        context: TurnExecutionContext | None = None,
    ) -> TurnResult:
        resolved_thread_id = thread_id or str(getattr(context, "thread_id", "") or "") or session_id or "coding"
        expected_context = TurnExecutionContext(
            thread_id=resolved_thread_id,
            session_id=session_id or "",
            runtime_profile=CODING_PROFILE,
        )
        if context is not None:
            identity_matches = (
                context.thread_id == expected_context.thread_id
                and context.session_id == expected_context.session_id
                and context.runtime_profile.profile_id == CODING_PROFILE.profile_id
            )
            if not identity_matches:
                raise ValueError("Coding turn context does not match thread, session, or profile")
        execution_context = context or expected_context
        thread = AgentThread(
            thread_id=resolved_thread_id,
            session_id=session_id or None,
        )
        return await self._runtime.run_turn(
            TurnRequest(
                thread=thread,
                user_text=user_text,
                runtime=None,
                context=execution_context,
            )
        )
