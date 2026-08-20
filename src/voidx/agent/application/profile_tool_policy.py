"""Build profile-pinned tool policies."""

from __future__ import annotations

from voidx.agent.domain.agent_profile import ResolvedAgentProfile
from voidx.agent.domain.tool_policy import CodingToolPolicy, ProfileToolPolicy


def profile_tool_policy_for(
    profile: ResolvedAgentProfile,
    *,
    baseline: object,
    phase: str,
    child_agent: bool = False,
) -> ProfileToolPolicy:
    return ProfileToolPolicy(
        baseline=baseline,
        resource_policy=profile.resource_policy,
        run_config=profile.run_config,
        snapshot_hash=profile.snapshot.snapshot_hash,
        phase=phase,
        child_agent=child_agent,
    )


def default_profile_tool_policy_for(
    profile: ResolvedAgentProfile, *, phase: str = "turn"
) -> ProfileToolPolicy:
    return profile_tool_policy_for(
        profile,
        baseline=CodingToolPolicy(),
        phase=phase,
    )
