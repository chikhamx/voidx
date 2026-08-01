"""Immutable context carried by one runtime turn."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from voidx.agent.domain.profile import RuntimeProfile


def _coding_profile() -> RuntimeProfile:
    return RuntimeProfile(profile_id="coding", revision=1, name="Coding")


class TurnExecutionContext(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    thread_id: str
    session_id: str
    runtime_profile: RuntimeProfile = Field(default_factory=_coding_profile)
    workspace: str = ""
    tool_policy: Any | None = None
    loop_controller: Any | None = None
    goal_controller: Any | None = None
    goal_intake_controller: Any | None = None
    goal_phase: str = "work"
