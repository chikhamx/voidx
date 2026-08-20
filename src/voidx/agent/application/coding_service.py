"""Application service for default Coding turns."""

from __future__ import annotations

from voidx.agent.application.runtime.contracts import TurnRequest, TurnResult
from voidx.agent.application.profile_tool_policy import default_profile_tool_policy_for
from voidx.agent.domain.profile import CODING_PROFILE
from voidx.agent.domain.thread import AgentThread
from voidx.agent.domain.turn_context import TurnExecutionContext


class CodingService:
    """Build coding-scoped turns and delegate execution to the AgentRuntime.

    The service owns coding thread identity, profile selection and TurnRequest
    construction. UI, slash command handling, and runtime persistence remain in
    the interactive service and runtime facade.
    """

    def __init__(self, runtime) -> None:
        self._runtime = runtime

    async def run_coding_turn(self, **kwargs):
        return await self.run_turn(**kwargs)

    async def run_turn(
        self,
        *,
        user_text: str,
        thread_id: str = "",
        session_id: str | None = None,
        context: TurnExecutionContext | None = None,
        display_text: str | None = None,
        workspace: str = "",
    ) -> TurnResult:
        resolved_thread_id = thread_id or str(getattr(context, "thread_id", "") or "") or session_id or "coding"
        if context is not None:
            identity_matches = (
                context.thread_id == resolved_thread_id
                and context.session_id == (session_id or "")
            )
            if not identity_matches:
                raise ValueError("Coding turn context does not match thread or session")
            execution_context = context
        else:
            from voidx.agent.application.agent_registry import agent_registry_for
            from voidx.agent.application.agent_profile_snapshot import restore_session_profile

            resolved = restore_session_profile(
                agent_registry_for(workspace or "."),
                profile_id="coding",
                snapshot=None,
            )
            execution_context = TurnExecutionContext(
                thread_id=resolved_thread_id,
                session_id=session_id or "",
                runtime_profile=resolved.runtime_profile,
                workspace=workspace,
                workflow_context=resolved.workflow_context,
                tool_policy=default_profile_tool_policy_for(resolved),
            )
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
                display_text=display_text,
            )
        )
