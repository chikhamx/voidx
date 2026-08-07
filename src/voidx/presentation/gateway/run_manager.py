"""Per-thread run manager for web/desktop session execution."""
from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from voidx.presentation.protocol import UiCancelCommand, UiCommand, UiResponse, UiSubmitCommand
from voidx.presentation.protocol.v2.envelope import ERR_CONCURRENCY_LIMIT, ERR_TURN_IN_PROGRESS
from voidx.presentation.protocol.v2.methods import MethodParamsError

ThreadStatus = Literal[
    "idle",
    "running",
    "waiting_for_user",
    "waiting_for_write_lock",
    "cancelling",
    "failed",
]
CommandHandler = Callable[[UiCommand], Awaitable[None] | None]

_ACTIVE_STATUSES = {"running", "waiting_for_user", "waiting_for_write_lock", "cancelling"}


@dataclass
class ThreadRunState:
    thread_id: str
    session_id: str = ""
    status: ThreadStatus = "idle"
    task: asyncio.Task[None] | None = None
    pending_requests: dict[str, asyncio.Future[UiResponse]] = field(default_factory=dict)
    model_provider: str = ""
    model_name: str = ""
    workspace: str = ""
    started_at: float = 0
    last_error: str = ""


class ThreadActor:
    """Serializes commands for exactly one thread/session."""

    def __init__(self, thread_id: str, command_handler: CommandHandler) -> None:
        self.thread_id = thread_id
        self._command_handler = command_handler
        self.mailbox: asyncio.Queue[UiCommand] = asyncio.Queue(maxsize=2)
        self.state = ThreadRunState(thread_id=thread_id, session_id=thread_id)

    async def submit(self, text: str) -> None:
        if self.is_active:
            raise MethodParamsError("turn already in progress", code=ERR_TURN_IN_PROGRESS)
        self.state.status = "running"
        self.state.started_at = time.time()
        await self._enqueue(UiSubmitCommand(text=text, thread_id=self.thread_id))

    async def cancel(self) -> None:
        if not self.is_active:
            self.state.status = "idle"
            return
        self.state.status = "cancelling"
        await self._enqueue(UiCancelCommand(thread_id=self.thread_id))

    @property
    def is_active(self) -> bool:
        return self.state.status in _ACTIVE_STATUSES

    def complete_turn(self) -> None:
        self.state.status = "idle"
        self.state.last_error = ""
        self.state.started_at = 0

    def fail_turn(self, message: str) -> None:
        self.state.status = "failed"
        self.state.last_error = message

    def mark_running(self) -> None:
        self.state.status = "running"
        if not self.state.started_at:
            self.state.started_at = time.time()

    def mark_idle(self) -> None:
        self.complete_turn()

    def register_pending_request(self, request_id: str, future: asyncio.Future[UiResponse]) -> None:
        self.state.pending_requests[request_id] = future
        if self.state.status == "running":
            self.state.status = "waiting_for_user"

    def resolve_pending_request(self, response: UiResponse) -> bool:
        future = self.state.pending_requests.pop(response.request_id, None)
        if future is None:
            return False
        if not future.done():
            future.set_result(response)
        if self.state.status == "waiting_for_user":
            self.state.status = "running"
        return True

    def remove_pending_request(self, request_id: str) -> None:
        self.state.pending_requests.pop(request_id, None)
        if self.state.status == "waiting_for_user" and not self.state.pending_requests:
            self.state.status = "running"

    async def _enqueue(self, command: UiCommand) -> None:
        self.mailbox.put_nowait(command)
        await self._drain_mailbox()

    async def _drain_mailbox(self) -> None:
        while not self.mailbox.empty():
            command = await self.mailbox.get()
            try:
                await self._handle(command)
            finally:
                self.mailbox.task_done()

    async def _handle(self, command: UiCommand) -> None:
        result = self._command_handler(command)
        if inspect.isawaitable(result):
            await result


class ThreadRunManager:
    """Coordinates per-thread actors and workspace-level concurrency accounting."""

    def __init__(self, *, command_handler: CommandHandler, max_concurrent_sessions: int = 2) -> None:
        self._command_handler = command_handler
        self._max_concurrent_sessions = max_concurrent_sessions
        self._actors: dict[str, ThreadActor] = {}
        self._workspace_write_lock_holder = ""
        self._workspace_write_lock_waiters: list[tuple[str, asyncio.Future[bool]]] = []

    def actor(self, thread_id: str) -> ThreadActor:
        if not thread_id:
            raise MethodParamsError("thread_id is required")
        actor = self._actors.get(thread_id)
        if actor is None:
            actor = ThreadActor(thread_id, self._command_handler)
            self._actors[thread_id] = actor
        return actor

    async def submit(self, thread_id: str, text: str) -> None:
        actor = self.actor(thread_id)
        if actor.is_active:
            raise MethodParamsError("turn already in progress", code=ERR_TURN_IN_PROGRESS)
        active = self.active_thread_ids()
        if thread_id not in active and len(active) >= self._max_concurrent_sessions:
            raise MethodParamsError("concurrency limit reached", code=ERR_CONCURRENCY_LIMIT)
        await actor.submit(text)

    async def cancel(self, thread_id: str) -> None:
        self._cancel_workspace_write_lock_waiter(thread_id)
        await self.actor(thread_id).cancel()

    async def acquire_workspace_write_lock(self, thread_id: str) -> bool:
        actor = self.actor(thread_id)
        if self._workspace_write_lock_holder in ("", thread_id):
            self._workspace_write_lock_holder = thread_id
            actor.mark_running()
            return True

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._workspace_write_lock_waiters.append((thread_id, future))
        actor.state.status = "waiting_for_write_lock"
        try:
            await future
        except asyncio.CancelledError:
            self._workspace_write_lock_waiters = [
                (waiting_thread_id, waiting_future)
                for waiting_thread_id, waiting_future in self._workspace_write_lock_waiters
                if waiting_future is not future
            ]
            raise
        actor.mark_running()
        return True

    def release_workspace_write_lock(self, thread_id: str) -> None:
        if self._workspace_write_lock_holder != thread_id:
            return
        self._workspace_write_lock_holder = ""
        self._promote_next_workspace_write_lock_waiter()

    def workspace_write_lock_holder(self) -> str:
        return self._workspace_write_lock_holder

    def _cancel_workspace_write_lock_waiter(self, thread_id: str) -> None:
        remaining: list[tuple[str, asyncio.Future[bool]]] = []
        for waiting_thread_id, future in self._workspace_write_lock_waiters:
            if waiting_thread_id == thread_id:
                if not future.done():
                    future.cancel()
            else:
                remaining.append((waiting_thread_id, future))
        self._workspace_write_lock_waiters = remaining

    def _promote_next_workspace_write_lock_waiter(self) -> None:
        while self._workspace_write_lock_waiters:
            thread_id, future = self._workspace_write_lock_waiters.pop(0)
            if future.cancelled() or future.done():
                continue
            self._workspace_write_lock_holder = thread_id
            self.actor(thread_id).mark_running()
            future.set_result(True)
            return

    def status(self, thread_id: str) -> ThreadStatus:
        actor = self._actors.get(thread_id)
        if actor is None:
            return "idle"
        return actor.state.status

    def complete_turn(self, thread_id: str) -> None:
        if self._workspace_write_lock_holder == thread_id:
            self.release_workspace_write_lock(thread_id)
        self.actor(thread_id).complete_turn()

    def fail_turn(self, thread_id: str, message: str) -> None:
        if self._workspace_write_lock_holder == thread_id:
            self.release_workspace_write_lock(thread_id)
        self._cancel_workspace_write_lock_waiter(thread_id)
        self.actor(thread_id).fail_turn(message)

    def mark_running(self, thread_id: str) -> None:
        self.actor(thread_id).mark_running()

    def mark_idle(self, thread_id: str) -> None:
        self.actor(thread_id).mark_idle()

    def register_pending_request(
        self,
        thread_id: str,
        request_id: str,
        future: asyncio.Future[UiResponse],
    ) -> None:
        self.actor(thread_id).register_pending_request(request_id, future)

    def resolve_pending_request(self, thread_id: str, response: UiResponse) -> bool:
        return self.actor(thread_id).resolve_pending_request(response)

    def resolve_unique_pending_request(self, response: UiResponse) -> bool:
        matches = [
            actor
            for actor in self._actors.values()
            if response.request_id in actor.state.pending_requests
        ]
        if len(matches) != 1:
            return False
        return matches[0].resolve_pending_request(response)

    def remove_pending_request(self, thread_id: str, request_id: str) -> None:
        actor = self._actors.get(thread_id)
        if actor is not None:
            actor.remove_pending_request(request_id)

    def active_thread_ids(self) -> list[str]:
        return sorted(
            thread_id
            for thread_id, actor in self._actors.items()
            if actor.is_active
        )


    def runtime_snapshot(self) -> dict[str, object]:
        return {
            "active_thread_ids": self.active_thread_ids(),
            "max_concurrent_sessions": self._max_concurrent_sessions,
        }

    def workspace_write_lock_snapshot(self) -> dict[str, object]:
        return {
            "holder_thread_id": self._workspace_write_lock_holder,
            "waiting_thread_ids": [
                thread_id
                for thread_id, future in self._workspace_write_lock_waiters
                if not future.done()
            ],
        }
