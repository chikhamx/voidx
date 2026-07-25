"""Generic per-turn tool visibility and authorization contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol


@dataclass(frozen=True)
class ToolPolicyDecision:
    allowed: bool
    reason: str
    requests_approval: bool = False


class ToolPolicy(Protocol):
    def visible_tool_ids(self, available_tool_ids: Iterable[str]) -> frozenset[str]: ...

    def check_tool_call(
        self, tool_name: str, args: Mapping[str, object]
    ) -> ToolPolicyDecision: ...


class CodingToolPolicy:
    is_coding_default = True

    def visible_tool_ids(self, available_tool_ids: Iterable[str]) -> frozenset[str]:
        return frozenset(available_tool_ids)

    def check_tool_call(
        self, tool_name: str, args: Mapping[str, object]
    ) -> ToolPolicyDecision:
        return ToolPolicyDecision(allowed=True, reason="coding_default")
