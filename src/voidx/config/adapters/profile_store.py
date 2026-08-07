"""Adapter implementing config's ``ModelProfileStore`` port over SQLite."""

from __future__ import annotations

from voidx.config.models import Profile
from voidx.config.adapters.profile_repository import (
    ModelProfileRow,
    delete_model_profile_async,
    get_model_profile_async,
    list_model_profiles_async,
    save_model_profile_async,
)


class MemoryModelProfileStore:
    async def list(self) -> list[Profile]:
        return [_to_profile(row) for row in await list_model_profiles_async()]

    async def get(self, name: str) -> Profile | None:
        row = await get_model_profile_async(name)
        return _to_profile(row) if row is not None else None

    async def save(self, profile: Profile) -> None:
        await save_model_profile_async(ModelProfileRow(
            name=profile.name,
            provider=profile.provider,
            model=profile.model,
            api_key=profile.api_key,
            base_url=profile.base_url,
            protocol=profile.protocol,
        ))

    async def delete(self, name: str) -> None:
        await delete_model_profile_async(name)


def _to_profile(row: ModelProfileRow) -> Profile:
    return Profile(
        name=row.name,
        api_key=row.api_key,
        base_url=row.base_url,
        protocol=row.protocol,
    )
