"""Domain contracts for runtime-backed /loop execution."""

from __future__ import annotations


from pydantic import BaseModel, ConfigDict, Field, field_validator

from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.domain.prompt_policy import LoopPromptPolicy
from voidx.runtime.task_state import LoopDecision, LoopMode, LoopSpec
from voidx.agent.domain.tool_view import BoundToolView


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




class LoopToolView(BoundToolView):
    workflow_enabled: bool = False
    phase: str = "work"

    @classmethod
    def default(cls, *, workflow_enabled: bool = False, phase: str = "work") -> "LoopToolView":
        return cls(workflow_enabled=workflow_enabled, phase=phase)

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
        if self.phase == "idle":
            allowed = {
                "loop",
                "read",
                "find",
                "search",
                "lsp",
                "document",
                "clarify",
            }
        if self.workflow_enabled and self.phase != "idle":
            allowed.update({"workflow", "todo"})
        return self.model_copy(update={"bound_tool_ids": frozenset(available & allowed)})
