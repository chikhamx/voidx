"""Domain contracts for runtime-backed /loop execution."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.prompt_policy import LoopPromptPolicy
from voidx.agent.domain.tool_view import BoundToolView


class LoopMode(str, Enum):
    FIXED = "fixed"
    DYNAMIC = "dynamic"


class LoopSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    prompt: str
    interval_seconds: float | None = Field(default=None, gt=0)
    workflow_enabled: bool = False
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
        return self.loop_thread_id(parent_thread_id)

    def prompt_summary(self) -> str:
        return self.prompt.replace("\n", " ")[:80]


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


LOOP_ITERATION_USER_TEXT = "Run the next scheduled loop iteration."
LOOP_PROFILE = RuntimeProfile(
    profile_id="loop", revision=1, name="Loop", protocol="loop",
    prompt_policy=LoopPromptPolicy(),
)

LOOP_IDLE_DIRECTIVE = """\
## Loop Idle Stage

This turn runs in loop mode while no autonomous loop is active. You may converse
with the user, answer questions with read-only tools, and help shape the next
LoopSpec — but you never execute the loop iterations themselves.

Hard rules:
- NEVER run an iteration: do not write code, do not run commands, do not call
  loop(operation='commit'). Iterations happen only inside the autonomous loop.
- You have read-only tools plus clarify and loop; no write or shell tools.
- When the user wants a loop to run, convert the request into a LoopSpec and call
  loop with op="init". loop(op="init") presents the spec for user approval; on
  revision feedback, update the spec and submit again. On cancel, drop it.
- Do not call loop with operation='start' or operation='commit'; those are
  iteration-only and not available in idle.
- Otherwise answer directly and conversationally.
"""


def loop_profile_for_base(base: RuntimeProfile, spec: LoopSpec) -> RuntimeProfile:
    """Overlay the loop iteration prompt onto any resolved profile.

    The profile's own system prompt (identity layer) is preserved; the loop
    instructions are appended. With the bundled loop profile this reduces to
    the legacy ``loop_profile_for_spec`` output.
    """
    system_prompt = "\n\n".join(
        part for part in (base.system_prompt, _loop_system_prompt(spec)) if part
    )
    return base.model_copy(update={"system_prompt": system_prompt})


def loop_profile_for_spec(spec: LoopSpec) -> RuntimeProfile:
    return loop_profile_for_base(LOOP_PROFILE, spec)


def _loop_system_prompt(spec: LoopSpec) -> str:
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


class LoopToolView(BoundToolView):
    workflow_enabled: bool = False
    phase: str = "work"

    @classmethod
    def default(cls, *, workflow_enabled: bool = False, phase: str = "work") -> "LoopToolView":
        return cls(workflow_enabled=workflow_enabled, phase=phase)

    def bind(self, available_tool_ids: set[str] | list[str] | tuple[str, ...]) -> "LoopToolView":
        available = set(available_tool_ids)
        allowed = {
            "loop", "read", "find", "search", "lsp", "document", "websearch",
            "webfetch", "mcp", "skill", "bash",
        }
        if self.phase == "idle":
            allowed = {"loop", "read", "find", "search", "lsp", "document", "clarify"}
        if self.workflow_enabled and self.phase != "idle":
            allowed.update({"workflow", "todo"})
        return self.model_copy(update={"bound_tool_ids": frozenset(available & allowed)})


__all__ = [
    "LoopDecision", "LoopMode", "LoopSpec", "LoopToolView", "LOOP_IDLE_DIRECTIVE",
    "LOOP_ITERATION_USER_TEXT", "LOOP_PROFILE", "loop_profile_for_spec",
]
