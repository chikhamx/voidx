"""Agent-only tool execution context and runtime capabilities."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import ConfigDict, Field, SkipValidation

from voidx.tooling.domain.context import ToolExecutionContext
from voidx.tooling.domain.interaction import UserInteraction, UserResponse
from voidx.tooling.domain.ui_events import ToolUiEventPublisher
from voidx.agent.ports.subagent import SubagentTransport


class InteractionCallback(Protocol):
    def __call__(self, interaction: UserInteraction) -> UserResponse | Awaitable[UserResponse]: ...


@dataclass
class AgentToolRuntime:
    loop_control: object | None = None
    goal_control: object | None = None
    goal_intake: object | None = None
    loop_intake: object | None = None
    subagent_transport: SubagentTransport | None = None
    run_id: str = ""
    workflow_repeat_state: dict[str, dict[str, int]] = field(default_factory=dict)
    interaction: InteractionCallback | None = None
    events: ToolUiEventPublisher | None = None
    access_grants: object | None = None
    revocation_epoch: object | None = None
    task_intent: str = "coding"
    goal_type: str = ""
    goal_target: str = ""
    active_workflow_names: list[str] = field(default_factory=list)
    workflow_runs: list[object] = field(default_factory=list)
    workflow_route: dict[str, str | None] | None = None
    goal_phase: str = "work"
    loop_phase: str = "work"


class AgentToolExecutionContext(ToolExecutionContext):
    runtime: SkipValidation[AgentToolRuntime] = Field(default_factory=AgentToolRuntime, exclude=True)
    model_config = ConfigDict(arbitrary_types_allowed=True)


__all__ = ["AgentToolExecutionContext", "AgentToolRuntime", "InteractionCallback"]
