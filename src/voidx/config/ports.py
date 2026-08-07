"""Ports owned by config and implemented by persistence adapters."""

from __future__ import annotations

from typing import Protocol

from voidx.config.models import Profile


class ModelProfileStore(Protocol):
    async def list(self) -> list[Profile]: ...
    async def get(self, name: str) -> Profile | None: ...
    async def save(self, profile: Profile) -> None: ...
    async def delete(self, name: str) -> None: ...
