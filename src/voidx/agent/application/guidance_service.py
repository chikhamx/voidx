"""Application service for the cross-mode durable Guidance inbox."""

from __future__ import annotations

import asyncio
import inspect
import threading
import uuid
from collections.abc import Callable
from typing import Any

from voidx.agent.domain.guidance import Guidance, GuidanceSource


class GuidanceService:
    """Submit and deliver Guidance through one durable store."""

    def __init__(
        self,
        store: Any,
        *,
        on_submitted: Callable[[Guidance], None] | None = None,
        id_factory: Callable[[], str] | None = None,
        max_chars: int = 2_000,
    ) -> None:
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        self._store = store
        self._on_submitted_callbacks: list[Callable[[Guidance], None]] = []
        if on_submitted is not None:
            self._on_submitted_callbacks.append(on_submitted)
        self._id_factory = id_factory or (lambda: f"guidance-{uuid.uuid4().hex[:20]}")
        self._max_chars = max_chars

    def can_submit_guidance(self) -> bool:
        return True

    def add_submitted_callback(self, callback: Callable[[Guidance], None]) -> None:
        if callback not in self._on_submitted_callbacks:
            self._on_submitted_callbacks.append(callback)

    def submit_guidance(
        self,
        text: str,
        *,
        source: GuidanceSource = "user",
        thread_id: str = "",
        session_id: str = "",
        run_id: str = "",
        phase: str | None = None,
    ) -> Guidance | None:
        normalized = " ".join(str(text).strip().split())
        if not normalized:
            return None
        truncated = len(normalized) > self._max_chars
        if truncated:
            normalized = normalized[: self._max_chars].rstrip()
        guidance = Guidance(
            guidance_id=self._id_factory(),
            text=normalized,
            truncated=truncated,
            source=source,
            target_thread_id=thread_id or None,
            target_session_id=session_id or None,
            target_run_id=run_id or None,
            target_phase=phase or None,
        )
        persisted = self._submit_sync(guidance)
        for callback in tuple(self._on_submitted_callbacks):
            callback(persisted)
        return persisted

    async def bind_delivery(
        self,
        delivery_id: str,
        *,
        session_id: str = "",
        thread_id: str = "",
        run_id: str = "",
        phase: str | None = None,
    ) -> list[Guidance]:
        target_kwargs = {
            key: value
            for key, value in {
                "session_id": session_id,
                "thread_id": thread_id,
                "run_id": run_id,
                "phase": phase,
            }.items()
            if value not in ("", None)
        }
        return await self._store.bind_guidance(delivery_id, **target_kwargs)

    async def release_delivery(self, delivery_id: str) -> None:
        await self._store.release_guidance(delivery_id)

    async def commit_delivery(self, delivery_id: str) -> None:
        await self._store.consume_guidance(delivery_id)

    def _submit_sync(self, guidance: Guidance) -> Guidance:
        submit_sync = getattr(self._store, "submit_guidance_sync", None)
        if callable(submit_sync):
            return submit_sync(guidance)
        result = self._store.submit_guidance(guidance)
        if not inspect.isawaitable(result):
            return result
        return _await_sync(result)


def _await_sync(awaitable: Any) -> Any:
    """Resolve an async store operation while preserving the sync port contract."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    result: list[Any] = []
    error: list[BaseException] = []

    def run() -> None:
        try:
            result.append(asyncio.run(awaitable))
        except BaseException as exc:
            error.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join()
    if error:
        raise error[0]
    return result[0] if result else None
