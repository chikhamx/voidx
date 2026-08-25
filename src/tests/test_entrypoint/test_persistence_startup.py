from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_build_settings_migrates_before_constructing_profile_store(monkeypatch) -> None:
    events: list[str] = []

    class ProfileStore:
        def __init__(self) -> None:
            events.append("profile_store")

    class Settings:
        @classmethod
        async def create(cls, workspace, *, profile_store):
            events.append("settings")
            assert isinstance(profile_store, ProfileStore)
            return cls()

    monkeypatch.setattr(
        "voidx.bootstrap.persistence.initialize_persistence",
        lambda: events.append("migration"),
    )
    async def reconcile_goal_cleanup() -> None:
        events.append("goal_cleanup")

    monkeypatch.setattr(
        "voidx.bootstrap.persistence.reconcile_goal_cleanup",
        reconcile_goal_cleanup,
    )
    async def deliver_goal_public_summaries() -> None:
        events.append("goal_summaries")

    monkeypatch.setattr(
        "voidx.bootstrap.persistence.deliver_goal_public_summaries",
        deliver_goal_public_summaries,
    )
    monkeypatch.setattr("voidx.config.Settings", Settings)
    monkeypatch.setattr(
        "voidx.config.adapters.profile_store.MemoryModelProfileStore",
        ProfileStore,
    )

    from voidx.bootstrap.application import build_settings

    await build_settings("${WORKSPACE}")

    assert events == [
        "migration",
        "goal_cleanup",
        "goal_summaries",
        "profile_store",
        "settings",
    ]
