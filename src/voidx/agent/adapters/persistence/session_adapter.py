"""Presentation-facing adapter for session persistence operations."""

from __future__ import annotations

from voidx.agent.adapters.persistence import session_repository


class SessionRepositoryAdapter:
    async def create_session(self, workspace: str = ".", provider: str = "anthropic", model: str = "", **kwargs: object):
        return await session_repository.create_session(
            workspace=workspace,
            provider=provider,
            model=model,
            **kwargs,
        )

    async def get_session(self, session_id: str):
        return await session_repository.get_session(session_id)

    async def list_sessions(self, limit: int = 50):
        return await session_repository.list_sessions(limit=limit)

    async def fork_session(self, session_id: str, *, title: str | None = None):
        return await session_repository.fork_session(session_id, title=title)

    async def delete_session(self, session_id: str) -> None:
        await session_repository.delete_session(session_id)

    async def update_title(self, session_id: str, title: str) -> None:
        await session_repository.update_title(session_id, title)

    async def update_session_model(self, session_id: str, provider: str, model: str) -> None:
        await session_repository.update_session_model(session_id, provider, model)

    async def update_session_profile(self, session_id: str, profile: str) -> None:
        await session_repository.update_session_profile(session_id, profile)
