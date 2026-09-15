"""Opt-in owner-envelope projection for direct children only.

The envelope remains the turn owner; resolver(event) supplies the actual legacy
(child integer ID, display name), never inferred from semantic IDs. Successful
starts resolve once. Failed starts may retry the resolver, which must be pure.
Nested children and child-owned assistant/tool output are not supported by this
owner-only bridge. Parent tools must be observed, active `agent` calls. Metadata
retains the entire semantic envelope and step ID; legacy statistics stay unknown.
All turn terminals require explicit child finishes, including cancellation: the
bridge cannot invent business facts or leave a successful turn with running nodes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from voidx.agent.domain import semantic_events as s
from voidx.agent.domain import ui_events as ui
from voidx.presentation.adapters.semantic_interaction_projector import ProjectionResult
from voidx.presentation.adapters.semantic_tool_projector import ToolProjectionState

SUPPORTED = (s.SubagentStarted, s.SubagentStepStarted, s.SubagentFinished)
SubagentResolver = Callable[[s.SubagentStarted], tuple[int, str]]


@dataclass
class _Child:
    payload: s.SubagentPayload
    agent_id: int
    name: str
    steps: set[str] = field(default_factory=set)
    finished: bool = False


@dataclass
class SubagentProjectionState:
    children: dict[str, _Child] = field(default_factory=dict)

    def validate_terminal(self) -> None:
        if any(not child.finished for child in self.children.values()):
            raise ValueError("Cannot end turn with pending subagents")


class SemanticSubagentProjector:
    def __init__(self, resolver: SubagentResolver | None = None) -> None:
        self.resolver = resolver

    def project(self, event, state: SubagentProjectionState, tools: ToolProjectionState,
                common: dict, reserved_ids: set[int]) -> ProjectionResult:
        p = event.payload
        owner = event.agent_id
        if p.parent_agent_id != owner:
            raise ValueError("Only direct children of the envelope owner are supported")
        if event.parent_tool_call_id not in (None, p.parent_tool_call_id):
            raise ValueError("Parent tool envelope and payload disagree")
        child = state.children.get(p.subagent_id)
        if isinstance(event, s.SubagentStarted):
            if child is not None:
                raise ValueError("Duplicate subagent start")
            if p.parent_tool_call_id is not None:
                tool = tools.tools.get(p.parent_tool_call_id)
                if tool is None or tool.name != "agent" or tool.ok is not None:
                    raise ValueError("Parent must be an observed active agent tool")
                if any(c.payload.parent_tool_call_id == p.parent_tool_call_id for c in state.children.values()):
                    raise ValueError("Parent tool already has a subagent")
            if self.resolver is None:
                raise ValueError("An explicit subagent resolver is required")
            agent_id, name = self.resolver(event.model_copy(deep=True))
            if type(agent_id) is not int or agent_id < 0 or agent_id in reserved_ids:
                raise ValueError("Invalid or colliding legacy child agent_id")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Resolver must supply a display name")
            child = _Child(p.model_copy(deep=True), agent_id, name)
            output = ui.SubagentStarted(thread_id=event.thread_id, agent_id=agent_id,
                subagent_id=p.subagent_id, name=name, description=p.description,
                parent_agent_id=owner if owner is not None else -1,
                parent_tool_call_id=p.parent_tool_call_id or "")
            state.children[p.subagent_id] = child
        else:
            if child is None or child.finished:
                raise ValueError("Subagent requires an active start")
            if any(getattr(p, key) != getattr(child.payload, key) for key in
                   ("parent_agent_id", "parent_tool_call_id", "description")):
                raise ValueError("Subagent parent/description changed")
            identity = dict(thread_id=event.thread_id, agent_id=child.agent_id, subagent_id=p.subagent_id)
            if isinstance(event, s.SubagentStepStarted):
                if p.step_id in child.steps:
                    raise ValueError("Duplicate subagent step")
                output = ui.SubagentStepStarted(**identity, name=child.name)
                child.steps.add(p.step_id)
            else:
                output = ui.SubagentFinished(**identity, ok=p.ok, finish_reason=p.reason, summary=p.summary)
                child.finished = True
        return ProjectionResult(events=(output,), subagent=event.model_copy(deep=True))
