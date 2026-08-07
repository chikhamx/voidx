"""Interactive run loop for the agent graph."""

from __future__ import annotations

import asyncio
from typing import Any

from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.ports.presentation import GuidancePort
from voidx.agent.ports.input import AutonomousInputRouter, InputStatusReader, SlashCommandDispatcher


class RunLoopStartupError(RuntimeError):
    """Raised when the run loop cannot start the selected frontend."""




# Commands that render their own turn bubble (via display_text) when they run a
# turn; the generic pre-dispatch echo would duplicate that bubble.
_SELF_DISPLAYING_COMMANDS = frozenset({"/loop", "/init"})


class AgentService:
    """Application-level startup and interactive run-loop service."""

    def __init__(
        self,
        status_reader: InputStatusReader,
        slash_dispatcher: SlashCommandDispatcher,
        autonomous_router: AutonomousInputRouter,
        guidance: GuidancePort,
    ) -> None:
        self._status_reader = status_reader
        self._slash_dispatcher = slash_dispatcher
        self._autonomous_router = autonomous_router
        self._guidance = guidance


    def can_submit_guidance(self) -> bool:
        return self._guidance.can_submit_guidance()

    def submit_guidance(self, text: str, **kwargs: Any) -> bool:
        return self._guidance.submit_guidance(text, **kwargs)

    async def run_coding_turn(
        self,
        user_text: str,
        *,
        thread_id: str = "",
        context: TurnExecutionContext | None = None,
        display_text: str | None = None,
    ) -> None:
        await self._autonomous_router.run_coding_turn(
            user_text,
            thread_id=thread_id,
            context=context,
            display_text=display_text,
        )



    async def dispatch_input(
        self,
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
            is_quiet = self._slash_dispatcher.consume_quiet_command(user_input)
            if is_quiet:
                self._slash_dispatcher.hide_command_output()
            self_displays = user_input.split(maxsplit=1)[0] in _SELF_DISPLAYING_COMMANDS
            if not is_quiet and not self_displays:
                self._autonomous_router.start_turn(user_input)
            dispatched = await self._slash_dispatcher.dispatch_slash(user_input)
            if not dispatched:
                self._autonomous_router.publish_message(f"[dim]Unknown command: {user_input}  — type [cyan]/help[/cyan] to see available commands[/dim]")
            if is_quiet:
                self._slash_dispatcher.hide_command_output()
            return True, None

        try:
            if await self._autonomous_router.route_chat_turn(user_input, thread_id=thread_id):
                return True, None
            if await self._autonomous_router.route_first_message(user_input, thread_id=thread_id):
                return True, None
            if await self._autonomous_router.route_followup(user_input, thread_id=thread_id):
                return True, None
            await self.run_coding_turn(
                user_text=user_input,
                thread_id=thread_id,
                context=context,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            self._autonomous_router.publish_message("\n[dim]Interrupted.[/dim]")
        return True, None
