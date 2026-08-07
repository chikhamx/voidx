"""Interactive run loop for the agent graph."""

from __future__ import annotations

import asyncio
from typing import Any

from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.application.coding_service import CODING_PROFILE
from voidx.agent.ports.execution_host import ExecutionHost
from voidx.agent.ports.presentation import AgentEventPublisher, NullAgentEventPublisher
from voidx.agent.domain.thread import AgentThread


AgentExecution = ExecutionHost


class RunLoopStartupError(RuntimeError):
    """Raised when the run loop cannot start the selected frontend."""




# Commands that render their own turn bubble (via display_text) when they run a
# turn; the generic pre-dispatch echo would duplicate that bubble.
_SELF_DISPLAYING_COMMANDS = frozenset({"/loop", "/init"})


class AgentService:
    """Application-level startup and interactive run-loop service."""

    def __init__(
        self,
        execution: AgentExecution,
        runtime,
        *,
        chat_service=None,
        coding_service=None,
        events: AgentEventPublisher | None = None,
    ) -> None:
        self._execution = execution
        self._runtime = runtime
        self._chat_service = chat_service
        self._coding_service = coding_service
        self._events = events or NullAgentEventPublisher()
        bind_coding_turn = getattr(execution, "bind_coding_turn_runner", None)
        if bind_coding_turn is not None:
            bind_coding_turn(self.run_coding_turn)


    def can_submit_guidance(self) -> bool:
        return callable(getattr(self._execution, "submit_guidance", None))

    def submit_guidance(self, text: str, **kwargs: Any) -> bool:
        submit = getattr(self._execution, "submit_guidance", None)
        if not callable(submit):
            return False
        return bool(submit(text, **kwargs))

    async def run_coding_turn(
        self,
        user_text: str,
        *,
        thread_id: str = "",
        context: TurnExecutionContext | None = None,
        display_text: str | None = None,
    ) -> None:
        if self._coding_service is None:
            raise RuntimeError("coding service is not configured")
        session_id = (
            (getattr(context, "session_id", "") or None)
            if context is not None
            else (self._execution.session_id or None)
        )
        await self._coding_service.run_turn(
            user_text=user_text,
            thread_id=thread_id,
            session_id=session_id,
            context=context,
            display_text=display_text,
            workspace=self._execution.workspace,
        )


    async def _handle_user_input(
        self,
        app,
        user_input: str,
        *,
        context: TurnExecutionContext | None = None,
        thread_id: str = "",
    ) -> tuple[bool, str | None]:
        user_input = user_input.strip()
        if not user_input:
            return True, None

        if user_input.startswith("/"):
            if user_input in ("/exit", "/quit"):
                return False, "\n[dim]bye.[/dim]"
            is_quiet = app.consume_quiet_command(user_input)
            hide_command_output = getattr(app, "hide_command_output", None)
            if is_quiet and callable(hide_command_output):
                hide_command_output()
            self_displays = user_input.split(maxsplit=1)[0] in _SELF_DISPLAYING_COMMANDS
            if not is_quiet and not self_displays:
                self._events.start_turn(user_input)
            dispatched = await self._dispatch_slash(user_input)
            if not dispatched:
                self._events.publish_message(f"[dim]Unknown command: {user_input}  — type [cyan]/help[/cyan] to see available commands[/dim]")
            if is_quiet and callable(hide_command_output):
                hide_command_output()
            return True, None

        try:
            if await self._route_chat_turn(user_input, thread_id=thread_id):
                return True, None
            if await self._route_autonomous_first_message(user_input, thread_id=thread_id):
                return True, None
            if await self._route_autonomous_followup(user_input, thread_id=thread_id):
                return True, None
            await self.run_coding_turn(
                user_text=user_input,
                thread_id=thread_id,
                context=context,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            self._events.publish_message("\n[dim]Interrupted.[/dim]")
        return True, None

    async def _route_autonomous_first_message(self, user_input: str, *, thread_id: str) -> bool:
        """Start a goal/loop from the first message of a goal/loop-profile session.

        Only the first message (session has no messages yet) is consumed as the
        prompt. Later messages are handled by `_route_autonomous_followup`.
        """
        session = getattr(self._execution, "session", None)
        profile = getattr(session, "runtime_profile", "coding")
        if profile not in {"goal", "loop"}:
            return False
        message_count = getattr(session, "message_count", 0) or 0
        if message_count > 0:
            return False
        if profile == "goal":
            return await self._handle_goal_first_message(user_input, thread_id=thread_id)
        return await self._handle_loop_first_message(user_input, thread_id=thread_id)

    async def _route_autonomous_followup(self, user_input: str, *, thread_id: str) -> bool:
        """Keep goal/loop host sessions from silently falling back to coding.

        Autonomous runtimes own a dedicated thread. Follow-up host messages are
        treated as guidance when possible; otherwise the user is told how to
        inspect or stop the active goal/loop instead of starting a coding turn.
        """
        del thread_id
        session = getattr(self._execution, "session", None)
        profile = getattr(session, "runtime_profile", "coding")
        if profile not in {"goal", "loop"}:
            return False

        parent = getattr(session, "id", None) or getattr(self._execution, "session_id", None)
        service = getattr(self._execution, f"{profile}_service", None)
        status = None
        if service is not None and hasattr(service, "status"):
            try:
                status = await service.status(parent)
            except Exception:
                status = None

        if status is not None and getattr(status, "active", False):
            if self.submit_guidance(user_input, source="user"):
                label = getattr(status, "objective_summary", None) or getattr(status, "prompt_summary", None) or profile
                self._events.publish_message(
                    f"[dim]/{profile} guidance queued for [cyan]{label}[/cyan]. "
                    f"Use /{profile} status or /{profile} stop.[/dim]"
                )
                return True
            label = getattr(status, "objective_summary", None) or getattr(status, "prompt_summary", None) or profile
            self._events.publish_message(
                f"[dim]/{profile} is active: [cyan]{label}[/cyan]. "
                f"Use /guide <text> for mid-run guidance, /{profile} status, or /{profile} stop. "
                f"Switch with /coding if you want a normal coding turn.[/dim]"
            )
            return True

        if profile == "goal":
            await self._run_goal_idle_turn(user_input, parent=parent)
            return True
        if profile == "loop":
            await self._run_loop_idle_turn(user_input, parent=parent)
            return True

        self._events.publish_message(
            f"[dim]This session is in {profile} mode. "
            f"Send a first message or /{profile} <args> to start, "
            f"or /coding to switch to coding.[/dim]"
        )
        return True

    async def _run_goal_idle_turn(self, user_input: str, *, parent: str | None) -> None:
        """Run a conversational goal-profile turn in the host session.

        When no goal is active, the session is conversational: the turn may
        answer directly, or submit a GoalSpec via goal(op="init") which starts
        the autonomous loop. The session stays in goal mode after it ends.
        """
        from voidx.agent.application.automation.goal.goal_idle import GoalIdleTurnService
        from voidx.agent.domain.thread import AgentThread

        goal_service = getattr(self._execution, "goal_service", None)
        if goal_service is None:
            return
        session = getattr(self._execution, "session", None)
        workspace = (
            getattr(session, "workspace", None)
            or getattr(session, "directory", None)
            or getattr(self._execution, "workspace", "")
            or ""
        )
        thread = AgentThread(
            thread_id=getattr(session, "id", None) or parent or "goal",
            session_id=getattr(session, "id", None) or parent or "",
            workspace=workspace,
        )
        idle = GoalIdleTurnService(self._runtime, goal_service)
        status = await idle.run(user_input, thread, parent_thread_id=parent)
        if status is not None and getattr(status, "active", False):
            self._events.publish_message(
                f"[dim]/goal started: [cyan]{status.objective_summary}[/cyan] "
                f"attempt {status.attempt_count}/{status.max_attempts}[/dim]"
            )

    async def _run_loop_idle_turn(self, user_input: str, *, parent: str | None) -> None:
        """Run a conversational loop-profile turn in the host session.

        When no loop is active, the session is conversational: the turn may
        answer directly, or submit a LoopSpec via loop(op="init") which starts
        the autonomous loop. The session stays in loop mode after it ends.
        """
        from voidx.agent.application.automation.loop.loop_idle import LoopIdleTurnService
        from voidx.agent.domain.thread import AgentThread

        loop_service = getattr(self._execution, "loop_service", None)
        if loop_service is None:
            return
        session = getattr(self._execution, "session", None)
        workspace = (
            getattr(session, "workspace", None)
            or getattr(session, "directory", None)
            or getattr(self._execution, "workspace", "")
            or ""
        )
        thread = AgentThread(
            thread_id=getattr(session, "id", None) or parent or "loop",
            session_id=getattr(session, "id", None) or parent or "",
            workspace=workspace,
        )
        idle = LoopIdleTurnService(self._runtime, loop_service)
        status = await idle.run(user_input, thread, parent_thread_id=parent)
        if status is not None and getattr(status, "active", False):
            self._events.publish_message(
                f"[dim]/loop started: [cyan]{status.prompt_summary}[/cyan] "
                f"· {status.loop_thread_id}[/dim]"
            )

    async def _route_chat_turn(self, user_input: str, *, thread_id: str) -> bool:
        """Route a turn to ChatService when the target thread is a chat session.

        Returns True when the turn was handled by the chat profile. Coding
        sessions and unknown threads fall through to the default coding path
        (False). With no explicit thread_id, the host's current session decides:
        a resumed chat session is routed to ChatService, anything else to coding.
        """
        if self._chat_service is None:
            return False
        target_id = thread_id or self._execution.session_id or ""
        if not target_id:
            return False
        from voidx.agent.adapters.persistence.session_repository import get_session

        target = await get_session(target_id)
        if target is None or target.runtime_profile != "chat":
            return False
        workspace = target.workspace or target.directory or None
        await self._chat_service.run_turn(
            user_text=user_input,
            thread=AgentThread(
                thread_id=f"chat:{target.id}",
                session_id=target.id,
            ),
            workspace=workspace,
        )
        return True

    async def _persist_first_message(self, user_input: str) -> None:
        """Save the consumed first message to the host session.

        The autonomous intake/start turns run with persist_user_input=False, so
        this records the prompt in the host session and bumps message_count to
        keep the first-message dispatch from firing again.
        """
        session = getattr(self._execution, "session", None)
        if session is None:
            return
        from datetime import datetime, timezone
        from voidx.agent.adapters.persistence.session_repository import save_message
        from voidx.agent.adapters.persistence.session_repository import MessageRow

        await save_message(
            MessageRow(
                session_id=session.id,
                role="user",
                content=user_input,
                content_format="text",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        session.message_count = (getattr(session, "message_count", 0) or 0) + 1

    async def _handle_loop_first_message(self, user_input: str, *, thread_id: str) -> bool:
        """Run the first message as an in-session loop idle turn.

        The turn may start a loop via loop(op="init") or simply converse; either
        way the session stays in loop mode. A persistent user-input record keeps
        the first message in the host session history.
        """
        service = getattr(self._execution, "loop_service", None)
        if service is None:
            return False
        parent = thread_id or self._execution.session_id or ""
        if not parent:
            return False
        await self._run_loop_idle_turn(user_input, parent=parent)
        await self._persist_first_message(user_input)
        return True

    async def _handle_goal_first_message(self, user_input: str, *, thread_id: str) -> bool:
        """Run the first message as an in-session goal idle turn.

        The turn may start a goal via goal(op="init") or simply converse; either
        way the session stays in goal mode. A persistent user-input record keeps
        the first message in the host session history.
        """
        goal_service = getattr(self._execution, "goal_service", None)
        if goal_service is None:
            return False
        parent = thread_id or self._execution.session_id or ""
        if not parent:
            return False
        await self._run_goal_idle_turn(user_input, parent=parent)
        await self._persist_first_message(user_input)
        return True

    async def _dispatch_slash(self, inp: str) -> bool:
        """Try to dispatch a slash command. Returns True if handled."""
        return await self._execution.slash.dispatch(inp)
