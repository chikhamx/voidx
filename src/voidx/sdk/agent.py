"""Single-run, explicitly owned headless SDK lifecycle."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing

from voidx.agent.domain.semantic_events import SemanticEvent
from voidx.bootstrap.headless import build_headless_run, headless_initialization
from voidx.config import Config, Settings


class VoidxAgent:
    def __init__(self, config: Config, *, settings: Settings | None = None) -> None:
        self._config = config
        self._settings = settings
        self._closed = False
        self._active = False
        self._run = None
        self._interactions = None
        self._initializing = None
        self._initialization_identity = None
        self._initialization_cancelled = False

    async def __aenter__(self):
        self._ensure_open()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("VoidxAgent is closed")

    async def stream(self, prompt: str, *, session_id: str = "",
                     workspace: str = ".") -> AsyncIterator[SemanticEvent]:
        self._ensure_open()
        if self._active:
            raise RuntimeError("Only one active stream is allowed per agent")
        self._active = True
        self._initialization_cancelled = False
        try:
            with headless_initialization(session_id) as identity:
                self._initialization_identity = identity
                self._initializing = asyncio.create_task(build_headless_run(
                    self._config, settings=self._settings, prompt=prompt,
                    session_id=session_id, workspace=workspace,
                ))
            try:
                self._run, self._interactions = await self._initializing
            except asyncio.CancelledError:
                if self._initialization_cancelled:
                    return
                raise
            self._initializing = None
            if self._closed:
                await self._run.cancel()
                return
            async with aclosing(self._run.events()) as events:
                async for event in events:
                    yield event
        finally:
            try:
                if self._run is not None:
                    if hasattr(self._run, "owner"):
                        await self._run._terminate()
                    else:
                        await self._run.cancel()
            finally:
                self._initializing = None
                self._run = None
                self._interactions = None
                self._active = False

    async def submit_interaction(self, interaction_id, response) -> bool:
        self._ensure_open()
        if self._interactions is None:
            return False
        return await self._interactions.submit_interaction(interaction_id, response)

    async def _cancel_initialization(self):
        task = self._initializing
        if task is None or task.done():
            return False
        if not self._initialization_cancelled:
            self._initialization_cancelled = True
            task.cancel()
        interrupted = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                if asyncio.current_task().cancelling():
                    interrupted = error
        if not task.cancelled():
            task.result()
        if interrupted is not None:
            raise interrupted
        return True

    async def cancel(self, *, session_id: str = "") -> None:
        if (self._initializing is not None and session_id
                and (self._initialization_identity is None
                     or session_id != self._initialization_identity.session_id)):
            raise ValueError("session_id does not match the active run")
        if await self._cancel_initialization():
            return
        if self._initializing is not None:
            run, _ = await asyncio.shield(self._initializing)
        else:
            run = self._run
        if run is None:
            return
        if session_id and session_id != (run.session_id if hasattr(run, "owner") else run.channel.identity["session_id"]):
            raise ValueError("session_id does not match the active run")
        await run.cancel()

    async def aclose(self) -> None:
        self._closed = True
        if await self._cancel_initialization():
            return
        if self._initializing is not None:
            run, _ = await asyncio.shield(self._initializing)
        else:
            run = self._run
        if run is not None:
            await run.cancel()
            if hasattr(run, "owner"):
                if (await run.owner.completion).outcome == "failed":
                    raise RuntimeError("Headless run failed while closing; see run diagnostics")
                return
            completion = await run.channel.completion
            from voidx.agent.domain.semantic_events import decode_event
            if decode_event(completion.terminal).kind == "turn.failed":
                raise RuntimeError("Headless run failed while closing; see run diagnostics")
