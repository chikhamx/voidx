"""Bridge agent tool policy contracts to the tooling registry catalog."""

from __future__ import annotations

from collections.abc import Mapping

from voidx.agent.domain.tool_policy import ProfileToolPolicy, ToolPolicyDecision
from voidx.tooling.domain.tool_names import canonical_tool_name


def check_tool_policy(
    policy: object,
    registry: object,
    tool_name: str,
    args: Mapping[str, object],
) -> ToolPolicyDecision:
    canonical = canonical_tool_name(tool_name)
    get_def = getattr(registry, "get_def", None)
    tool_def = get_def(canonical) if callable(get_def) else None
    capability = getattr(tool_def, "capability", None)
    capability_id = str(capability) if capability is not None else None
    if isinstance(policy, ProfileToolPolicy):
        return policy.check_tool_call(canonical, args, capability=capability_id)
    return policy.check_tool_call(canonical, args)


def policy_allows(policy: object, registry: object, tool_name: str) -> bool:
    get_def = getattr(registry, "get_def", None)
    tool_def = get_def(tool_name) if callable(get_def) else None
    capability = getattr(tool_def, "capability", None)
    capability_id = str(capability) if capability is not None else None
    allows = getattr(policy, "allows")
    if isinstance(policy, ProfileToolPolicy):
        return bool(allows(tool_name, capability=capability_id))
    return bool(allows(tool_name))


def tool_policy_metadata(decision: ToolPolicyDecision) -> dict[str, object]:
    return {
        "snapshot_hash": decision.snapshot_hash,
        "phase": decision.phase,
        "decision": "allow" if decision.allowed else "deny",
        "reason": decision.reason,
        "canonical_tool": decision.canonical_tool,
        "capability": decision.capability or "",
    }
