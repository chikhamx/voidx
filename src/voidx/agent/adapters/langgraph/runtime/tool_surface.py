"""Unified LLM-visible tool surface resolution.

`ToolRegistry` is the executable catalog; this module computes the actual tool
definitions bound to one LLM call from catalog + profile + phase + policy +
protocol + child constraints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from voidx.agent.application.tool_filters import (
    filter_unavailable_lsp_tools,
    strip_gemini_unsupported_schema_keys,
)
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.turn_context import TurnExecutionContext
from voidx.agent.adapters.langgraph.runtime.control_protocol import (
    resolve_control_protocol,
)

EXECUTION_ONLY_TOOLS = frozenset({"git", "lsp_format", "compact"})
CHILD_BLOCKED_TOOLS = frozenset({"agent", "clarify", "checkpoint"})
LIFECYCLE_TOOLS = frozenset({"turn", "goal", "loop"})

_GOAL_PHASES = frozenset({"idle", "intake", "work", "evaluator"})
_LOOP_PHASES = frozenset({"idle", "work"})
_GOAL_VISIBLE_PHASES = frozenset({"idle", "intake", "evaluator"})


class ToolNamePolicy(Protocol):
    """Resolver 依赖的最小 policy 接口；不绑定任何具体 ToolView 实现。"""

    def allows(self, tool_name: str) -> bool: ...


@dataclass(frozen=True)
class ToolSurfaceContext:
    runtime_profile: RuntimeProfile | None
    goal_phase: str | None = None
    loop_phase: str | None = None
    tool_policy: ToolNamePolicy | None = None
    turn_context: TurnExecutionContext | None = None
    child_agent: bool = False
    lsp_manager: object | None = None
    model_protocol: str | None = None


@dataclass(frozen=True)
class ToolSurface:
    definitions: list[dict[str, Any]]
    dropped: dict[str, str] = field(default_factory=dict)


def lifecycle_phase(context: ToolSurfaceContext) -> tuple[str, str] | None:
    """Resolve the active lifecycle (protocol, phase); None means unknown/minimal."""
    protocol_id = resolve_control_protocol(context.runtime_profile).protocol_id
    if protocol_id == "goal":
        phase = context.goal_phase
        return (protocol_id, phase) if phase in _GOAL_PHASES else None
    if protocol_id == "loop":
        phase = context.loop_phase
        return (protocol_id, phase) if phase in _LOOP_PHASES else None
    return (protocol_id, "")


def _definition_name(tool: dict[str, Any]) -> str:
    name = tool.get("name")
    if name:
        return str(name)
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return ""


def resolve_tool_surface(registry, context: ToolSurfaceContext) -> ToolSurface:
    dropped: dict[str, str] = {}
    definitions = list(registry.serialize_definitions())

    def reject(reason: str, names: frozenset[str]) -> None:
        nonlocal definitions
        kept = []
        for tool in definitions:
            name = _definition_name(tool)
            if name in names:
                dropped[name] = reason
            else:
                kept.append(tool)
        definitions = kept

    # Execution-only tools stay in the catalog but never reach the LLM surface.
    reject("execution_only", EXECUTION_ONLY_TOOLS)

    # Lifecycle definitions come from the control protocol, never the catalog.
    reject("lifecycle_catalog", LIFECYCLE_TOOLS)

    if context.child_agent:
        reject("child_blocked", CHILD_BLOCKED_TOOLS)

    policy = context.tool_policy
    if policy is not None:
        reject("policy", frozenset(
            name for name in (_definition_name(t) for t in definitions) if not policy.allows(name)
        ))

    protocol = resolve_control_protocol(context.runtime_profile)
    lifecycle = lifecycle_phase(context)
    injected: list[dict[str, Any]] = []
    if not context.child_agent and lifecycle is not None:
        protocol_id, phase = lifecycle
        if protocol_id == "turn":
            injected = protocol.tool_definitions()
        elif protocol_id == "loop":
            injected = protocol.tool_definitions()
        elif protocol_id == "goal" and phase in _GOAL_VISIBLE_PHASES:
            injected = protocol.tool_definitions()

    injected_names = frozenset(_definition_name(t) for t in injected)
    if injected_names:
        reject("protocol_override", injected_names)
    definitions.extend(injected)

    if policy is not None:
        reject("policy", frozenset(
            name for name in (_definition_name(t) for t in definitions) if not policy.allows(name)
        ))

    definitions = filter_unavailable_lsp_tools(definitions, context.lsp_manager)
    definitions = strip_gemini_unsupported_schema_keys(definitions, context.model_protocol)
    return ToolSurface(definitions=definitions, dropped=dropped)
