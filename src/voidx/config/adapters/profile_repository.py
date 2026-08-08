"""User-level model profile persistence."""

from __future__ import annotations

from pydantic import BaseModel, Field

from voidx.persistence.sqlite import execute_commit, fetch_all, fetch_one, now, write_transaction


class ModelProfileRow(BaseModel):
    name: str
    provider: str
    model: str
    api_key: str = ""
    base_url: str | None = None
    protocol: str | None = None


# ── async API ────────────────────────────────────────────────────────────


async def list_model_profiles_async() -> list[ModelProfileRow]:
    rows = await fetch_all(
        """SELECT name, provider, model, api_key, base_url, protocol
           FROM model_profiles
           ORDER BY updated_at DESC, name ASC"""
    )
    return [_row_to_profile(row) for row in rows]


async def get_model_profile_async(name: str) -> ModelProfileRow | None:
    row = await fetch_one(
        """SELECT name, provider, model, api_key, base_url, protocol
           FROM model_profiles
           WHERE name = ?""",
        (name,),
    )
    return _row_to_profile(row) if row else None


async def first_model_profile_for_provider_async(provider: str) -> ModelProfileRow | None:
    row = await fetch_one(
        """SELECT name, provider, model, api_key, base_url, protocol
           FROM model_profiles
           WHERE provider = ?
           ORDER BY updated_at DESC, name ASC
           LIMIT 1""",
        (provider,),
    )
    return _row_to_profile(row) if row else None


async def save_model_profile_async(profile: ModelProfileRow) -> None:
    timestamp = now()
    await execute_commit(
        """INSERT INTO model_profiles
               (name, provider, model, api_key, base_url, protocol, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   provider = excluded.provider,
                   model = excluded.model,
                   api_key = excluded.api_key,
                   base_url = excluded.base_url,
                   protocol = excluded.protocol,
                   updated_at = excluded.updated_at""",
        (
            profile.name,
            profile.provider,
            profile.model,
            profile.api_key,
            profile.base_url,
            profile.protocol,
            timestamp,
            timestamp,
        ),
    )


async def delete_model_profile_async(name: str) -> None:
    await execute_commit(
        "DELETE FROM model_profiles WHERE name = ?", (name,)
    )


def _row_to_profile(row) -> ModelProfileRow:
    return ModelProfileRow(
        name=row["name"],
        provider=row["provider"],
        model=row["model"],
        api_key=row["api_key"],
        base_url=row["base_url"],
        protocol=row["protocol"],
    )
