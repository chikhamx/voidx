"""Domain contracts for runtime-backed /loop execution."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from voidx.agent.domain.profile import RuntimeProfile


LOOP_ITERATION_USER_TEXT = "Run the next scheduled loop iteration."
LOOP_PROFILE = RuntimeProfile(profile_id="loop", revision=1, name="Loop", protocol="loop")


def loop_profile_for_spec(spec: "LoopSpec") -> RuntimeProfile:
    return LOOP_PROFILE.model_copy(
        update={"system_prompt": _loop_system_prompt(spec)}
    )


def _loop_system_prompt(spec: "LoopSpec") -> str:
    lines = [
        "## Loop Goal",
        spec.prompt.strip(),
        "",
        "## Loop Iteration Instructions",
        "Run one scheduled iteration toward the loop goal, then submit exactly one loop(operation='commit') decision.",
    ]
    if spec.interval_seconds is not None:
        lines.append(f"Use the fixed loop interval of {spec.interval_seconds:g} seconds for continue decisions.")
    else:
        lines.append("Choose the next delay based on progress and the loop goal.")
    return "\n".join(lines).strip()


class LoopMode(str, Enum):
    FIXED = "fixed"
    DYNAMIC = "dynamic"


class LoopDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Literal["continue", "completed", "blocked", "needs_user", "failed", "stop"]
    summary: str
    progress: Literal["none", "partial", "meaningful"] = "none"
    next_delay_seconds: float | None = None
    reason: str = ""

    @field_validator("summary")
    @classmethod
    def require_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary must not be empty")
        return value


class LoopSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    prompt: str
    interval_seconds: float | None = Field(default=None, gt=0)
    workflow_enabled: bool = False
    # Identifies this loop's thread/session: each /loop start gets a fresh
    # generation so it begins with an empty session; "active" marks legacy rows.
    generation: str = "active"

    @field_validator("prompt")
    @classmethod
    def require_prompt(cls, value: str) -> str:
        prompt = value.strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        return prompt

    @field_validator("generation")
    @classmethod
    def require_generation(cls, value: str) -> str:
        generation = value.strip()
        if not generation:
            raise ValueError("generation must not be empty")
        return generation

    @property
    def mode(self) -> LoopMode:
        return LoopMode.FIXED if self.interval_seconds is not None else LoopMode.DYNAMIC

    def loop_thread_id(self, parent_thread_id: str | None) -> str:
        parent = (parent_thread_id or "default").strip() or "default"
        return f"loop:{parent}:{self.generation}"

    def loop_session_id(self, parent_thread_id: str | None) -> str:
        # Loop history lives in its own session so it never reads or writes the
        # parent conversation; this keeps the two contexts fully isolated.
        return self.loop_thread_id(parent_thread_id)

    def prompt_summary(self) -> str:
        return self.prompt.replace("\n", " ")[:80]


class LoopToolView(BaseModel):
    model_config = ConfigDict(frozen=True)

    workflow_enabled: bool = False
    bound_tool_ids: frozenset[str] = Field(default_factory=frozenset)

    @classmethod
    def default(cls, *, workflow_enabled: bool = False) -> "LoopToolView":
        return cls(workflow_enabled=workflow_enabled)

    def bind(self, available_tool_ids: set[str] | list[str] | tuple[str, ...]) -> "LoopToolView":
        available = set(available_tool_ids)
        allowed = {
            "loop",
            "read",
            "find",
            "search",
            "lsp",
            "document",
            "websearch",
            "webfetch",
            "mcp",
            "skill",
            "bash",
        }
        if self.workflow_enabled:
            allowed.update({"workflow", "task_status", "todo"})
        return self.model_copy(update={"bound_tool_ids": frozenset(available & allowed)})

    def allows(self, tool_id: str, **_kwargs) -> bool:
        return tool_id in self.bound_tool_ids

    def visible_tool_ids(self, available_tool_ids) -> frozenset[str]:
        return frozenset(tool for tool in available_tool_ids if self.allows(tool))

    def check_tool_call(self, tool_id: str, _args) -> object:
        from voidx.agent.domain.tool_policy import ToolPolicyDecision

        return ToolPolicyDecision(self.allows(tool_id), "tool_bound" if self.allows(tool_id) else "tool_not_bound", False)
