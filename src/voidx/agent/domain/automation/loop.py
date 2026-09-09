"""Domain contracts for runtime-backed /loop execution."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.prompt_policy import LoopPromptPolicy
from voidx.agent.domain.thread import DecisionMetadata, LoopGuardrailState, RuntimeDecision
from voidx.agent.domain.tool_view import BoundToolView


class LoopMode(str, Enum):
    FIXED = "fixed"
    DYNAMIC = "dynamic"


class LoopSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    prompt: str
    interval_seconds: int | None = Field(default=None, ge=1)

    @field_validator("interval_seconds")
    @classmethod
    def validate_interval_seconds(cls, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("interval_seconds must be an integer >= 1")
        if value < 1:
            raise ValueError("interval_seconds must be >= 1")
        return value
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


LOOP_STALL_LIMIT = 5
LOOP_MISSING_DECISION_LIMIT = 3
NO_LOOP_DECISION_REASON = "no_loop_decision_submitted"


def apply_loop_guardrails(
    previous: RuntimeDecision | None, decision: RuntimeDecision
) -> RuntimeDecision:
    """Pause a loop that stalls or whose model repeatedly skips the commit decision.

    Consecutive progress="none" iterations and consecutive runtime fallback
    decisions are counted across iterations via decision metadata carried in
    the wakeup payload; hitting either limit rewrites the decision to
    needs_user so the loop pauses instead of burning iterations forever.
    """
    stall = 0
    missing = 0
    if (
        previous is not None
        and previous.metadata is not None
        and previous.metadata.loop_guardrail is not None
    ):
        stall = previous.metadata.loop_guardrail.stall_count
        missing = previous.metadata.loop_guardrail.missing_decision_count

    if decision.outcome == "continue":
        stall = stall + 1 if decision.progress == "none" else 0
        missing = missing + 1 if decision.reason == NO_LOOP_DECISION_REASON else 0
        if missing >= LOOP_MISSING_DECISION_LIMIT:
            decision = decision.model_copy(
                update={
                    "outcome": "needs_user",
                    "next_delay_seconds": None,
                    "reason": "loop_decision_missing",
                    "summary": (
                        f"Loop paused: {missing} consecutive iterations ended without "
                        f"a loop decision. Last summary: {decision.summary}"
                    ),
                }
            )
        elif stall >= LOOP_STALL_LIMIT:
            decision = decision.model_copy(
                update={
                    "outcome": "needs_user",
                    "next_delay_seconds": None,
                    "reason": "loop_stalled",
                    "summary": (
                        f"Loop paused: no progress for {stall} consecutive iterations. "
                        f"Last summary: {decision.summary}"
                    ),
                }
            )
    else:
        stall = 0
        missing = 0

    metadata = (decision.metadata or DecisionMetadata()).model_copy(
        update={
            "loop_guardrail": LoopGuardrailState(
                stall_count=stall, missing_decision_count=missing
            )
        }
    )
    return decision.model_copy(update={"metadata": metadata})


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
  loop_commit. Iterations happen only inside the autonomous loop.
- You have read-only tools plus clarify and loop_init; no write or shell tools.
- When the user wants a loop to run, convert the request into a LoopSpec and call
  loop_init. loop_init presents the spec for user approval; on revision feedback,
  update the spec and submit again. On cancel, drop it.
- Do not call loop_start or loop_commit; those are iteration-only and not available in idle.
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
        "Run one scheduled iteration toward the loop goal, then submit exactly one loop_commit decision.",
    ]
    if spec.interval_seconds is not None:
        lines.append(f"Use the fixed loop interval of {spec.interval_seconds:d} seconds for continue decisions.")
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
            "loop", "loop_start", "loop_commit", "read", "find", "search", "lsp", "document", "websearch",
            "webfetch", "mcp", "skill", "bash",
        }
        if self.phase == "idle":
            allowed = {"loop", "loop_init", "read", "find", "search", "lsp", "document", "clarify"}
        if self.workflow_enabled and self.phase != "idle":
            allowed.update({"workflow", "todo"})
        return self.model_copy(update={"bound_tool_ids": frozenset(available & allowed)})


__all__ = [
    "LoopDecision", "LoopMode", "LoopSpec", "LoopToolView", "LOOP_IDLE_DIRECTIVE",
    "LOOP_ITERATION_USER_TEXT", "LOOP_MISSING_DECISION_LIMIT", "LOOP_PROFILE",
    "LOOP_STALL_LIMIT", "NO_LOOP_DECISION_REASON", "apply_loop_guardrails",
    "loop_profile_for_base", "loop_profile_for_spec",
]
