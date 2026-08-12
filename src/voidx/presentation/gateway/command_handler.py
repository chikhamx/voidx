"""Web gateway command adapter for agent application input."""

from __future__ import annotations

from typing import Protocol

from voidx.agent.domain.automation.goal import GOAL_PROFILE
from voidx.agent.domain.automation.loop import LOOP_PROFILE
from voidx.agent.domain.profile import CHAT_PROFILE, CODING_PROFILE, RuntimeProfile
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.ports.presentation import GatewayThreadRegistry, GuidancePort, RuntimeStatusReader
from voidx.presentation.protocol import UiCancelCommand, UiSubmitCommand, parse_ui_command


class GatewayFrontend(Protocol):
    def submit_external_input(self, text: str, *, context: TurnExecutionContext) -> None: ...
    def cancel_external_input(self, *, context: TurnExecutionContext) -> None: ...


class GatewayCommandHandler:
    def __init__(
        self,
        status_reader: RuntimeStatusReader,
        guidance: GuidancePort,
        thread_registry: GatewayThreadRegistry,
    ) -> None:
        self._status_reader = status_reader
        self._guidance = guidance
        self._thread_registry = thread_registry

    async def handle(self, app: GatewayFrontend, command: object) -> None:
        status = self._status_reader.runtime_status()
        if isinstance(command, dict) and command.get("kind") == "guide":
            context = _guidance_context(
                str(command.get("thread_id", "")),
                status.session.session_id,
                status.workspace,
            )
            self._guidance.submit_guidance(
                str(command.get("text", "")),
                source="user",
                thread_id=context.thread_id,
                session_id=context.session_id,
            )
            return
        parsed = parse_ui_command(command)
        if isinstance(parsed, UiSubmitCommand):
            text = parsed.text
            if text.strip().startswith("/guide "):
                context = _guidance_context(
                    parsed.thread_id,
                    status.session.session_id,
                    status.workspace,
                )
                self._guidance.submit_guidance(
                    text.strip().removeprefix("/guide").strip(),
                    source="user",
                    thread_id=context.thread_id,
                    session_id=context.session_id,
                )
                return
            self._thread_registry.ensure_thread(status.session)
            session_id = parsed.session_id or (
                parsed.thread_id if parsed.thread_id else status.session.session_id
            )
            context = _turn_context(
                parsed.thread_id,
                session_id,
                parsed.workspace or status.workspace,
                parsed.runtime_profile,
            )
            app.submit_external_input(text, context=context)
        elif isinstance(parsed, UiCancelCommand):
            context = _turn_context(parsed.thread_id, status.session.session_id, status.workspace)
            app.cancel_external_input(context=context)




def _guidance_context(
    thread_id: str,
    active_session_id: str,
    workspace: str,
) -> TurnExecutionContext:
    if thread_id:
        return _turn_context(thread_id, thread_id, workspace)
    return _turn_context("", active_session_id, workspace)


def _runtime_profile(profile_id: str) -> RuntimeProfile:
    profiles = {
        "chat": CHAT_PROFILE,
        "coding": CODING_PROFILE,
        "goal": GOAL_PROFILE,
        "loop": LOOP_PROFILE,
    }
    return profiles.get(profile_id, CODING_PROFILE)


def _turn_context(
    thread_id: str,
    session_id: str,
    workspace: str,
    runtime_profile: str = "coding",
) -> TurnExecutionContext:
    return TurnExecutionContext(
        thread_id=thread_id or session_id or "coding",
        session_id=session_id,
        runtime_profile=_runtime_profile(runtime_profile),
        workspace=workspace,
    )
