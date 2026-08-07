"""Headless interaction frontend backed by the JSON-RPC gateway."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from voidx.presentation.output.events.schema import PermissionToolDetail
from voidx.presentation.output.types import SubmitHandler, UiStatus, coding_turn_context_for_queue
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.presentation.protocol.requests import UiChoiceRequest, UiPermissionRequest, UiTextRequest


def _normalize_choices(choices: list[str | tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    normalized: list[tuple[str, str, str]] = []
    for choice in choices:
        if isinstance(choice, str):
            normalized.append((choice, choice, ""))
        else:
            normalized.append(choice)
    return normalized


class _SubmitQueueItem(str):
    def __new__(
        cls,
        submit_text: str,
        *,
        thread_id: str = "",
        context: TurnExecutionContext | None = None,
    ):
        if context is None:
            context = TurnExecutionContext(thread_id=thread_id, session_id=thread_id)
        obj = str.__new__(cls, submit_text)
        obj.context = context
        obj.thread_id = context.thread_id
        return obj


class GatewayHeadlessFrontend:
    """InteractionFrontend for desktop/web headless mode.

    The desktop path talks to the core exclusively through gateway protocol
    requests and commands, so it must not instantiate or import the terminal UI.
    """

    def __init__(self, status: UiStatus, commands: list[tuple[str, str]]) -> None:
        self.status = status
        self.commands = commands
        self._queue: asyncio.Queue[_SubmitQueueItem | None] = asyncio.Queue()
        self._running = False
        self._current_submit_task: asyncio.Task[bool] | None = None
        self._current_submit_context: TurnExecutionContext | None = None
        self._submit_cancel_requested = False
        self._external_command_handler: Any = None
        self._external_request_handler: Any = None
        self._quiet_commands: list[str] = []

    async def run(self, on_submit: SubmitHandler) -> None:
        await self.run_headless(on_submit)

    async def run_headless(self, on_submit: SubmitHandler) -> None:
        self._running = True
        try:
            while self._running:
                item = await self._queue.get()
                if item is None:
                    return
                context = getattr(item, "context", None)
                if context is None:
                    thread_id = getattr(item, "thread_id", "")
                    context = coding_turn_context_for_queue(self.status, thread_id=thread_id)
                self._current_submit_context = context
                try:
                    submit_result = on_submit(str(item), context=context)
                except TypeError:
                    try:
                        submit_result = on_submit(str(item), thread_id=context.thread_id)
                    except TypeError:
                        submit_result = on_submit(str(item))
                self._current_submit_task = asyncio.create_task(submit_result)
                try:
                    keep_running = await self._current_submit_task
                except asyncio.CancelledError:
                    if not self._submit_cancel_requested:
                        raise
                    keep_running = True
                finally:
                    self._current_submit_task = None
                    self._current_submit_context = None
                    self._submit_cancel_requested = False
                if not keep_running:
                    self._running = False
                    return
        finally:
            self._running = False

    def submit_external_input(
        self,
        text: str,
        *,
        thread_id: str = "",
        context: TurnExecutionContext | None = None,
    ) -> None:
        context = coding_turn_context_for_queue(self.status, thread_id=thread_id, context=context)
        self._queue.put_nowait(_SubmitQueueItem(text, context=context))

    def cancel_external_input(
        self,
        *,
        thread_id: str = "",
        context: TurnExecutionContext | None = None,
    ) -> None:
        self._submit_cancel_requested = True
        if self._current_submit_task is not None:
            self._current_submit_task.cancel()

    def set_external_command_handler(self, handler: Any) -> None:
        self._external_command_handler = handler

    def set_external_request_handler(self, handler: Any) -> None:
        self._external_request_handler = handler

    async def ask_choice(
        self,
        prompt: str,
        choices: list[str | tuple[str, str, str]],
        selected: int = 0,
        anchor: str = "",
        details: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
    ) -> str | None:
        if self._external_request_handler is None:
            return None
        details_payload = [
            PermissionToolDetail.model_validate(item) for item in (details or [])
        ]
        request_id = f"choice_{uuid.uuid4().hex}"
        thread_id = self._current_thread_id()
        if details_payload:
            request = UiPermissionRequest(
                request_id=request_id,
                thread_id=thread_id,
                prompt=prompt,
                choices=_normalize_choices(choices),
                tools=details_payload,
            )
        else:
            request = UiChoiceRequest(
                request_id=request_id,
                thread_id=thread_id,
                prompt=prompt,
                choices=_normalize_choices(choices),
            )
        return await self._await_response(request, timeout=timeout)

    async def ask_text(
        self,
        prompt: str,
        default: str = "",
        secret: bool = False,
        timeout: float | None = None,
    ) -> str | None:
        if self._external_request_handler is None:
            return None
        request = UiTextRequest(
            request_id=f"text_{uuid.uuid4().hex}",
            thread_id=self._current_thread_id(),
            prompt=prompt,
            default=default,
            secret=secret,
        )
        return await self._await_response(request, timeout=timeout)

    async def _await_response(self, request: Any, *, timeout: float | None = None) -> str | None:
        response = self._external_request_handler(request)
        try:
            result = await response if timeout is None else await asyncio.wait_for(response, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if result is None:
            return None
        value = result.get("value") if isinstance(result, dict) else getattr(result, "value", None)
        return None if value is None else str(value)

    def _current_thread_id(self) -> str:
        context = self._current_submit_context
        return str(getattr(context, "thread_id", "") or "")

    def invalidate(self) -> None:
        return None

    def invalidate_skill_service_cache(self) -> None:
        return None

    def hide_command_output(self) -> None:
        return None

    def consume_quiet_command(self, command: str) -> bool:
        command = command.strip()
        try:
            index = self._quiet_commands.index(command)
        except ValueError:
            return False
        del self._quiet_commands[index]
        return True

    def queue_quiet_command(self, command: str) -> None:
        command = command.strip()
        if not command:
            return
        self._quiet_commands.append(command)
        self.submit_external_input(command)
