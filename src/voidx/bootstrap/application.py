"""Application-level construction with explicit adapter selection."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from voidx.config import Settings


async def build_settings(workspace: str = ".") -> Settings:
    from voidx.config import Settings
    from voidx.config.adapters.profile_store import MemoryModelProfileStore

    from voidx.bootstrap.persistence import initialize_persistence

    initialize_persistence()
    return await Settings.create(
        workspace,
        profile_store=MemoryModelProfileStore(),
    )


__all__ = ["build_settings"]
