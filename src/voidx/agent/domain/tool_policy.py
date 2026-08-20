"""Generic per-turn tool visibility and authorization contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol

from voidx.agent.domain.agent_profile import ResourcePolicy
from voidx.agent.domain.run_config import RunConfig


@dataclass(frozen=True)
class ToolPolicyDecision:
    allowed: bool
    reason: str
    requests_approval: bool = False
    canonical_tool: str = ""
    snapshot_hash: str = ""
    phase: str = ""
    capability: str | None = None


class ToolPolicy(Protocol):
    def visible_tool_ids(self, available_tool_ids: Iterable[str]) -> frozenset[str]: ...

    def check_tool_call(
        self, tool_name: str, args: Mapping[str, object]
    ) -> ToolPolicyDecision: ...


class CodingToolPolicy:
    is_coding_default = True

    def allows(self, tool_name: str) -> bool:
        return True

    def visible_tool_ids(self, available_tool_ids: Iterable[str]) -> frozenset[str]:
        return frozenset(available_tool_ids)

    def check_tool_call(
        self, tool_name: str, args: Mapping[str, object]
    ) -> ToolPolicyDecision:
        return ToolPolicyDecision(allowed=True, reason="coding_default")


@dataclass(frozen=True)
class ProfileToolPolicy:
    """One pinned policy used for both visibility and execution checks."""

    baseline: object
    resource_policy: ResourcePolicy
    run_config: RunConfig
    snapshot_hash: str
    phase: str
    child_agent: bool = False

    def allows(
        self,
        tool_name: str,
        *,
        capability: str | None = None,
    ) -> bool:
        return self.check_tool_call(
            tool_name, {}, capability=capability
        ).allowed

    @property
    def bound_tool_ids(self) -> frozenset[str]:
        """Compatibility view for prompt assembly using legacy BoundToolView.

        Runtime visibility and execution still use ``visible_tool_ids`` and
        ``check_tool_call`` as the security boundary. This property only exposes
        an enumerable baseline after applying the same profile restrictions.
        """
        baseline_ids = getattr(self.baseline, "bound_tool_ids", None)
        if baseline_ids is None:
            return frozenset()
        return self.visible_tool_ids(baseline_ids)

    def visible_tool_ids(
        self,
        available_tool_ids: Iterable[str] | Mapping[str, str],
    ) -> frozenset[str]:
        if isinstance(available_tool_ids, Mapping):
            return frozenset(
                tool_name
                for tool_name, capability in available_tool_ids.items()
                if self.allows(tool_name, capability=capability)
            )
        return frozenset(
            tool_name for tool_name in available_tool_ids if self.allows(tool_name)
        )

    def check_tool_call(
        self,
        tool_name: str,
        args: Mapping[str, object],
        *,
        capability: str | None = None,
    ) -> ToolPolicyDecision:
        canonical = tool_name
        reason = self._rejection_reason(canonical, capability)
        if reason:
            return self._decision(False, reason, canonical, capability)

        baseline_check = getattr(self.baseline, "check_tool_call", None)
        if callable(baseline_check):
            baseline = baseline_check(canonical, args)
            if not baseline.allowed:
                return self._decision(False, baseline.reason, canonical, capability)
            requests_approval = baseline.requests_approval
        else:
            baseline_allows = getattr(self.baseline, "allows", None)
            if callable(baseline_allows) and not baseline_allows(canonical):
                return self._decision(False, "tool_not_bound", canonical, capability)
            requests_approval = False
        return self._decision(
            True,
            "profile_allowed",
            canonical,
            capability,
            requests_approval=requests_approval,
        )

    def _rejection_reason(
        self, canonical: str, capability: str | None
    ) -> str:
        if canonical in self.resource_policy.tools_block:
            return "profile_blocked"
        lifecycle = {"turn", "goal", "loop"}
        if canonical in lifecycle and not self._lifecycle_allowed(canonical):
            return "lifecycle_not_allowed"
        if self.child_agent and canonical in {"agent", "clarify", "checkpoint"}:
            return "child_tool_blocked"
        if (
            self.resource_policy.hitl_mode == "autonomous"
            and capability == "hitl_interaction"
        ):
            return "hitl_interaction_unavailable"
        allowed = self.resource_policy.tools_allow
        if allowed is not None and canonical not in allowed:
            return "profile_not_allowed"
        return ""

    def _lifecycle_allowed(self, canonical: str) -> bool:
        if canonical != self.run_config.lifecycle_tool:
            return False
        if canonical == "goal":
            return self.phase in {"idle", "intake", "evaluator"}
        if canonical == "loop":
            return self.phase in {"idle", "work"}
        return canonical == "turn" and self.phase == "turn"

    def _decision(
        self,
        allowed: bool,
        reason: str,
        canonical: str,
        capability: ToolCapability | None,
        *,
        requests_approval: bool = False,
    ) -> ToolPolicyDecision:
        return ToolPolicyDecision(
            allowed=allowed,
            reason=reason,
            requests_approval=requests_approval,
            canonical_tool=canonical,
            snapshot_hash=self.snapshot_hash,
            phase=self.phase,
            capability=capability,
        )
