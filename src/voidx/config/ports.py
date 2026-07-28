"""Ports for services that config consumes from upper layers.

Config owns the interface; the memory layer provides the adapter
(``voidx.memory.profile_store.MemoryModelProfileStore``) and the composition
root binds it at startup via :func:`bind_model_profile_store`.
"""

from __future__ import annotations

from typing import Protocol

from voidx.config.models import Profile


class ModelProfileStore(Protocol):
    async def list(self) -> list[Profile]: ...
    async def get(self, name: str) -> Profile | None: ...
    async def save(self, profile: Profile) -> None: ...
    async def delete(self, name: str) -> None: ...


_store: ModelProfileStore | None = None


def bind_model_profile_store(store: ModelProfileStore) -> None:
    global _store
    _store = store


def model_profile_store() -> ModelProfileStore:
    global _store
    if _store is None:
        from voidx.memory.profile_store import MemoryModelProfileStore

        _store = MemoryModelProfileStore()
    return _store
