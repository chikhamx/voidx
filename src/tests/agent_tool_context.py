from __future__ import annotations

from dataclasses import replace
from typing import Any

from voidx.agent.adapters.tools.context import AgentToolExecutionContext, AgentToolRuntime


_AGENT_RUNTIME_FIELDS = (
    "task_intent",
    "goal_type",
    "goal_target",
    "active_workflow_names",
    "workflow_runs",
    "workflow_route",
    "goal_phase",
    "loop_phase",
)


def agent_tool_context(**values: Any) -> AgentToolExecutionContext:
    runtime = values.pop("runtime", None) or AgentToolRuntime()
    runtime_values = {
        name: values.pop(name)
        for name in _AGENT_RUNTIME_FIELDS
        if name in values
    }
    return AgentToolExecutionContext(runtime=replace(runtime, **runtime_values), **values)


__all__ = ["agent_tool_context"]
