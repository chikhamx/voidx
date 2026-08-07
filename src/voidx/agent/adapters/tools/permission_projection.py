"""Project Agent-specific invocation arguments into Tooling permission values."""

from __future__ import annotations

from enum import Enum


class AgentInvocationClass(str, Enum):
    READONLY = "readonly"
    IMPLEMENT = "implement"


def classify_agent_invocation(args: dict) -> AgentInvocationClass:
    """Resolve legacy agent/persona/mode inputs into a stable permission class."""
    explicit_persona = str(args.get("persona") or "")
    mode = str(args.get("mode") or "").strip().lower()
    delegated = str(args.get("name") or args.get("agent") or "")
    if explicit_persona == "implement" or mode == "implement" or delegated == "implement":
        return AgentInvocationClass.IMPLEMENT
    return AgentInvocationClass.READONLY


def project_agent_permission_args(args: dict) -> dict:
    """Attach the resolved class consumed by generic Tooling policy."""
    invocation_class = classify_agent_invocation(args)
    return {**args, "invocation_class": invocation_class.value}


def project_agent_tool_call(tool_call: dict) -> dict:
    if str(tool_call.get("name") or "") != "agent":
        return tool_call
    args = tool_call.get("args")
    normalized_args = args if isinstance(args, dict) else {}
    return {**tool_call, "args": project_agent_permission_args(normalized_args)}
