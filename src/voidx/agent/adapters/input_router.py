"""LangGraph adapter for autonomous goal/loop input routing."""

from __future__ import annotations

from typing import Any

from voidx.agent.ports.presentation import AgentEventPublisher, GuidancePort


class LangGraphAutonomousInputRouter:
    def __init__(
        self,
        host: Any,
        runtime: Any,
        events: AgentEventPublisher,
        guidance: GuidancePort,
        *,
        chat_service: Any,
        coding_service: Any,
        loop_service: Any,
        goal_service: Any,
    ) -> None:
        self._execution = host
        self._runtime = runtime
        self._events = events
        self._guidance = guidance
        self._chat_service = chat_service
        self._coding_service = coding_service
        self._loop_service = loop_service
        self._goal_service = goal_service


    def start_turn(self, text: str) -> None:
        self._events.start_turn(text)

    def publish_message(self, message: str) -> None:
        self._events.publish_message(message)

    async def run_coding_turn(
        self,
        text: str,
        *,
        thread_id: str = "",
        context: Any = None,
        display_text: str | None = None,
    ) -> None:
        if self._coding_service is None:
            raise RuntimeError("coding service is not configured")
        session = getattr(self._execution, "session", None)
        session_id = (
            (getattr(context, "session_id", "") or None)
            if context is not None
            else (getattr(self._execution, "session_id", "") or None)
        )
        workspace = getattr(self._execution, "workspace", "")
        await self._coding_service.run_coding_turn(
            user_text=text,
            thread_id=thread_id,
            session_id=session_id,
            context=context,
            display_text=display_text,
            workspace=workspace,
        )

    async def route_chat_turn(self, text: str, *, thread_id: str = "") -> bool:
        if self._chat_service is None:
            return False
        target_id = thread_id or getattr(self._execution, "session_id", "") or ""
        if not target_id:
            return False
        from voidx.agent.adapters.persistence.session_repository import get_session
        from voidx.agent.domain.thread import AgentThread

        target = await get_session(target_id)
        if target is None or target.runtime_profile != "chat":
            return False
        await self._chat_service.run_chat_turn(
            user_text=text,
            thread=AgentThread(thread_id=f"chat:{target.id}", session_id=target.id),
            workspace=target.workspace or target.directory or None,
        )
        return True

    async def route_first_message(self, user_input: str, *, thread_id: str) -> bool:
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
            from voidx.agent.adapters.persistence.message_rows import is_guidance_row
            from voidx.agent.adapters.persistence.session_repository import load_messages

            rows = await load_messages(session.id)
            if any(not is_guidance_row(row) for row in rows):
                return False
        if profile == "goal":
            return await self._handle_goal_first_message(user_input, thread_id=thread_id)
        return await self._handle_loop_first_message(user_input, thread_id=thread_id)

    async def route_followup(self, user_input: str, *, thread_id: str) -> bool:
        """Keep goal/loop host sessions from silently falling back to coding.

        Autonomous runtimes own a dedicated thread. Follow-up host messages are
        treated as guidance when possible; otherwise the user is told how to
        inspect or stop the active goal/loop instead of starting a coding turn.
        """
        session = getattr(self._execution, "session", None)
        profile = getattr(session, "runtime_profile", "coding")
        if profile not in {"goal", "loop"}:
            return False

        parent = getattr(session, "id", None) or getattr(self._execution, "session_id", None)
        service = self._goal_service if profile == "goal" else self._loop_service
        status = None
        if service is not None and hasattr(service, "status"):
            try:
                status = await service.status(parent)
            except Exception:
                status = None

        if status is not None and getattr(status, "active", False):
            target_thread_id = (
                getattr(status, f"{profile}_thread_id", "")
                or thread_id
                or parent
                or ""
            )
            if self._guidance.submit_guidance(
                user_input,
                source="user",
                thread_id=target_thread_id,
                session_id=target_thread_id,
            ):
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

        goal_service = self._goal_service
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

        loop_service = self._loop_service
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
        service = self._loop_service
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
        goal_service = self._goal_service
        if goal_service is None:
            return False
        parent = thread_id or self._execution.session_id or ""
        if not parent:
            return False
        await self._run_goal_idle_turn(user_input, parent=parent)
        await self._persist_first_message(user_input)
        return True
