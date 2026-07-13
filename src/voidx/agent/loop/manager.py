"""Async scheduler for session-scoped /loop prompts."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from voidx.agent.loop.prompt_source import PromptSource


class LoopManager:
    def __init__(
        self,
        host: Any,
        *,
        idle_event: asyncio.Event,
        workspace: str,
        default_interval_seconds: float = 600,
    ) -> None:
        self._host = host
        self._idle_event = idle_event
        self._workspace = workspace
        self._default_interval_seconds = default_interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._prompt_source: PromptSource | None = None
        self._interval_seconds: float | None = None
        self._bash_tool: Any | None = None
        self._ctx: Any | None = None
        self._session_id: str | None = None
        self._next_dynamic_delay: float | None = None
        self._wakeup_changed = asyncio.Event()
        self._next_fire_at: float | None = None
        self._last_error: str | None = None

    def set_workspace(self, workspace: str) -> None:
        self._workspace = workspace

    def start(
        self,
        prompt_source: PromptSource,
        interval_seconds: float | None,
        *,
        bash_tool: Any | None = None,
        ctx: Any | None = None,
        session_id: str | None = None,
    ) -> None:
        self.stop()
        self._prompt_source = prompt_source
        self._interval_seconds = interval_seconds
        self._bash_tool = bash_tool
        self._ctx = ctx
        self._session_id = session_id
        self._next_dynamic_delay = None
        self._wakeup_changed.clear()
        self._last_error = None
        self._task = asyncio.create_task(self._run_loop(), name="voidx-loop-manager")

    def stop(self) -> None:
        task = self._task
        self._task = None
        self._next_fire_at = None
        if task is not None and not task.done():
            task.cancel()
        self._wakeup_changed.set()

    def status(self) -> dict | None:
        task = self._task
        if task is None or task.done():
            return None
        mode = "dynamic" if self._interval_seconds is None else "fixed"
        return {
            "active": True,
            "mode": mode,
            "interval_seconds": self._interval_seconds,
            "default_interval_seconds": self._default_interval_seconds,
            "next_fire_at": self._next_fire_at,
            "next_fire_in_seconds": self._remaining_seconds(),
            "prompt_summary": self._prompt_summary(),
            "session_id": self._session_id,
            "last_error": self._last_error,
        }

    def schedule_wakeup(self, delay_seconds: float | None = None, *, stop: bool = False) -> None:
        if stop:
            self.stop()
            return
        if delay_seconds is None:
            raise ValueError("delay_seconds is required unless stop=True")
        self._next_dynamic_delay = delay_seconds
        self._wakeup_changed.set()

    async def cleanup(self) -> None:
        task = self._task
        self.stop()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self._last_error = f"loop crashed: {exc}"

    async def _run_loop(self) -> None:
        while True:
            delay = self._next_delay()
            await self._sleep_interruptibly(delay)
            await self._idle_event.wait()
            prompt_source = self._prompt_source
            if prompt_source is None:
                return
            prompt = await prompt_source.resolve(
                self._workspace,
                bash_tool=self._bash_tool,
                ctx=self._ctx,
            )
            if prompt.startswith("[loop] prompt source error:"):
                self._last_error = prompt
            try:
                await self._host.run_synthetic_turn(prompt, display_text=self._display_text(prompt))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"loop turn failed: {exc}"

    def _next_delay(self) -> float:
        if self._interval_seconds is not None:
            return self._interval_seconds
        delay = self._next_dynamic_delay
        self._next_dynamic_delay = None
        return delay if delay is not None else self._default_interval_seconds

    async def _sleep_interruptibly(self, delay: float) -> None:
        while True:
            self._wakeup_changed.clear()
            self._next_fire_at = time.monotonic() + delay
            try:
                await asyncio.wait_for(self._wakeup_changed.wait(), timeout=delay)
            except asyncio.TimeoutError:
                self._next_fire_at = None
                return
            if self._task is None:
                return
            if self._interval_seconds is not None:
                continue
            delay = self._next_delay()

    def _remaining_seconds(self) -> float | None:
        if self._next_fire_at is None:
            return None
        return max(0.0, self._next_fire_at - time.monotonic())

    def _prompt_summary(self) -> str:
        if self._prompt_source is None:
            return ""
        return self._prompt_source.raw.replace("\n", " ")[:80]

    @staticmethod
    def _display_text(prompt: str) -> str:
        summary = prompt.replace("\n", " ")[:80]
        return f"[loop] {summary}"
