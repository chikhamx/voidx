"""Best-effort persistence for autonomous results published to a parent session."""

from __future__ import annotations

import asyncio
import logging

from voidx.agent.adapters.persistence.session_repository import MessageRow, save_message
from voidx.persistence.sqlite import now as memory_now


class AsyncParentResultPublisher:
    """Schedule parent-session result persistence without blocking the caller."""

    def publish(self, parent_thread_id: str, text: str) -> None:
        asyncio.get_running_loop().create_task(self._save(parent_thread_id, text))

    async def _save(self, parent_thread_id: str, text: str) -> None:
        try:
            await save_message(
                MessageRow(
                    session_id=parent_thread_id,
                    role="assistant",
                    content=text,
                    content_format="text",
                    created_at=memory_now(),
                )
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "failed to publish autonomous result to parent session %s",
                parent_thread_id,
            )


__all__ = ["AsyncParentResultPublisher"]
