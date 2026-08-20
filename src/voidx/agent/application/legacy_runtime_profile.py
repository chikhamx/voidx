"""Compatibility mapping for bundled legacy runtime profile ids."""

from __future__ import annotations

from voidx.agent.domain.automation.goal import GOAL_PROFILE
from voidx.agent.domain.automation.loop import LOOP_PROFILE
from voidx.agent.domain.profile import CHAT_PROFILE, CODING_PROFILE, RuntimeProfile


_BUNDLED_LEGACY_PROFILES = {
    profile.profile_id: profile
    for profile in (CODING_PROFILE, CHAT_PROFILE, GOAL_PROFILE, LOOP_PROFILE)
}


def bundled_legacy_runtime_profile(
    *,
    source: str,
    profile_id: str,
    revision: int,
) -> RuntimeProfile | None:
    if source != "bundled":
        return None
    profile = _BUNDLED_LEGACY_PROFILES.get(profile_id)
    if profile is None or profile.revision != revision:
        return None
    return profile


__all__ = ["bundled_legacy_runtime_profile"]
