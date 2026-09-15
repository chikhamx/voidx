"""Own producers, persistence and cleanup; write exactly one run terminal."""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from voidx.agent.domain import semantic_events as e
from voidx.agent.application.runtime.semantic_channel import SemanticChannel
from voidx.observability import log_internal_error
from voidx.agent.ports.run_lifecycle import RunLifecycle

Outcome = Literal["completed", "failed", "cancelled"]


@dataclass(frozen=True)
class RunResult:
    completed: e.TurnCompletedPayload


class RunSupervisor:
    """All producers must run in execute or via spawn, never detached tasks.

    cleanup runs after producers stop. Its interaction coordinator must return
    only unresolved requests whose required publication successfully returned;
    it owns ID/ownership validation and exactly-once resolution. This class
    validates the bounded tail's wire shape, not interaction registry state.
    Cleanup must not publish into the business queue (which may be full).
    Persistence is called for a successful execution result, before completion.
    """

    def __init__(
        self, channel: SemanticChannel, *,
        execute: Callable[[RunSupervisor], Awaitable[RunResult]],
        persist: Callable[[RunResult], Awaitable[None]],
        cleanup: Callable[[Outcome], Awaitable[Sequence[e.InteractionResolved]]],
        before_cancel: Callable[[], None] | None = None,
        owner: RunLifecycle | None = None,
    ) -> None:
        self.channel = channel
        self._owner = owner
        self._owned_children: set[asyncio.Task[Any]] = set()
        self._execute = execute
        self._persist = persist
        self._cleanup = cleanup
        self._before_cancel = before_cancel
        self._task: asyncio.Task[None] | None = None
        self._work: asyncio.Task[RunResult] | None = None
        self._group: asyncio.TaskGroup | None = None
        self.cancelling = False
        self._cancel_failed = False
        # Fail initialization rather than promise an unencodable failure terminal.
        channel._snapshot(self._terminal("failed"), 1)
        channel._snapshot(self._terminal("cancelled"), 1)

    def _terminal(self, outcome: Outcome, result: RunResult | None = None) -> e.SemanticEvent:
        envelope = dict(
            **self.channel.identity, event_id=uuid4(), sequence=1,
            agent_id=None, parent_tool_call_id=None, timestamp=time.time(),
        )
        if outcome == "completed":
            if result is None:
                raise ValueError("Completed requires a structured result")
            return e.TurnCompleted(**envelope, payload=result.completed)
        if outcome == "cancelled":
            return e.TurnCancelled(**envelope, payload=e.ReasonPayload(reason="cancelled"))
        return e.TurnFailed(**envelope, payload=e.DiagnosticPayload(
            code="run_failed", summary="Run failed", recoverable=False,
        ))

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._drive(), name="semantic-run-supervisor")

    def spawn(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        if self._owner is not None:
            coroutine.close()
            raise RuntimeError("Owned producers require spawn_factory capacity admission")
        if self.cancelling or self._group is None:
            coroutine.close()
            raise RuntimeError("Run is not accepting producers")
        return self._group.create_task(coroutine)

    def spawn_factory(self, factory: Callable[[], Coroutine[Any, Any, Any]]) -> asyncio.Task[Any]:
        if self.cancelling or self._group is None:
            raise RuntimeError("Run is not accepting producers")
        if self._owner is not None:
            task = self._owner.spawn(factory)
            self._owned_children.add(task)
            task.add_done_callback(self._owned_children.discard)
            return task
        return self._group.create_task(factory())

    async def _produce_and_persist(self) -> RunResult:
        async with asyncio.TaskGroup() as group:
            self._group = group
            try:
                result = await self._execute(self)
                if self._owned_children:
                    await asyncio.gather(*tuple(self._owned_children))
            finally:
                self._group = None
                children = tuple(self._owned_children)
                for child in children:
                    child.cancel()
                if children:
                    await asyncio.gather(*children, return_exceptions=True)
        if not isinstance(result, RunResult):
            raise TypeError("execute must return RunResult")
        await self._persist(result)
        return result

    async def _drive(self) -> None:
        outcome: Outcome = "cancelled" if self.cancelling else "completed"
        result = None
        if not self.cancelling:
            self._work = asyncio.create_task(self._produce_and_persist(), name="semantic-run-execute")
            try:
                result = await self._work
            except asyncio.CancelledError:
                outcome = "cancelled"
            except Exception as exc:
                log_internal_error(
                    exc, context="run_supervisor_execute_persist",
                    session_id=self.channel.identity["session_id"],
                )
                # Public diagnostics deliberately never include exception text.
                outcome = "failed"
        if self._cancel_failed:
            outcome = "failed"
        self.channel._stop_publishing()
        try:
            tail = await self._cleanup(outcome)
        except (Exception, asyncio.CancelledError) as exc:
            log_internal_error(
                exc, context="run_supervisor_cleanup",
                session_id=self.channel.identity["session_id"],
            )
            if self._owner is not None:
                self._owner.record_cleanup_error(exc)
            outcome, tail = "failed", ()
            for discard in self.channel._interaction_cleanups:
                discard()
        if self._cancel_failed:
            outcome = "failed"
        elif self.cancelling and outcome == "completed":
            outcome = "cancelled"
        try:
            self.channel._finish(self._terminal(outcome, result), tail)
        except (ValueError, TypeError) as exc:
            log_internal_error(
                exc, context="run_supervisor_finish",
                session_id=self.channel.identity["session_id"],
            )
            for discard in self.channel._interaction_cleanups:
                discard()
            self.channel._finish(self._terminal("failed"))

    async def wait(self) -> None:
        self.start()
        assert self._task is not None
        await asyncio.shield(self._task)

    def begin_cancel(self) -> None:
        if not self.cancelling:
            self.cancelling = True
            if self._before_cancel is not None:
                try:
                    self._before_cancel()
                except Exception as exc:
                    self._cancel_failed = True
                    log_internal_error(
                        exc, context="run_supervisor_before_cancel",
                        session_id=self.channel.identity["session_id"],
                    )
            if self._work is not None:
                self._work.cancel()

    async def cancel(self) -> None:
        if self._task is not None and self._task.done():
            await self.wait()
            return
        self.begin_cancel()
        await self.wait()

    async def events(self) -> AsyncIterator[e.SemanticEvent]:
        self.start()
        iterator = self.channel.events()
        try:
            async for event in iterator:
                yield event
        finally:
            await iterator.aclose()
            await self.cancel()
