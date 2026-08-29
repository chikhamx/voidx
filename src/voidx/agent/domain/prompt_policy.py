"""Profile-scoped prompt policies and domain prompt contracts."""

from __future__ import annotations

from typing import Protocol

from voidx.agent.domain.prompt_contracts import BaseSystemProfile, CHAT_PROFILE_SPEC, ContextSection


GOAL_INTAKE_DIRECTIVE = """\
## Goal Intake Stage

This turn is the intake stage of an autonomous Goal. Its sole responsibility is to
produce a GoalSpec from the user's request — never to execute the request itself.

- Permitted outcomes: call clarify with one targeted question, or call goal_init with
  a complete spec.
- Forbidden: performing the task, producing the requested analysis/answer, writing
  code, or running commands for the task. The work phase starts only after intake.
- goal_init presents the spec for user approval; on revision feedback, update the
  spec and submit again.
"""

GOAL_EVALUATOR_DIRECTIVE = """\
## Goal Evaluator Stage

This turn is the evaluator stage of an autonomous Goal. Its sole responsibility is
to judge whether the work-phase evidence satisfies the acceptance condition, submit
exactly one lifecycle decision, and then summarize that decision for the user.

Follow this procedure:
1. Review — read the work-phase evidence in this turn's input and check each
   acceptance condition against it. The work phase already ran; never re-run the
   task, and do not output a final acceptance report before the decision is durable.
2. Verify — spot-check any evidence that looks missing or unreliable with read-only
   tools (read, find, search, lsp, document). You have no execution tools; do not
   attempt to run commands.
3. Decide — call goal_decision:
   - status="finished" when every condition is backed by concrete evidence;
   - status="continue" when evidence is insufficient — name the missing evidence
     in the reason so the next work attempt collects it;
   - status="blocked" when the goal cannot proceed.
   In the reason field, cite the evidence or files you relied on.
4. Summarize — after the tool confirms that the decision is durable, call no more
   tools. Give the user one concise, natural final response reflecting the decision.
"""

GOAL_IDLE_DIRECTIVE = """\
## Goal Idle Stage

This turn runs in goal mode while no autonomous goal is active. You may converse
with the user, answer questions with read-only tools, and help shape the next
GoalSpec — but you never execute the task itself.

Hard rules:
- NEVER perform the work: do not write code, do not run commands, do not produce
  the requested artifact. Work happens only inside the autonomous goal loop.
- You have read-only tools plus clarify and goal_init; no write or shell tools.
- When the user wants a goal to run, convert the request into a GoalSpec and call
  goal_init. It presents the spec for user approval; on revision feedback, update
  the spec and submit again. On cancel, drop it.
- Do not call goal_checkpoint or goal_decision; those are autonomous phases only.
- Otherwise answer directly and conversationally.
"""

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


class PromptPolicy(Protocol):
    def base_system_spec(self) -> BaseSystemProfile | None: ...
    def profile_sections(self, turn_context: object | None) -> list[ContextSection]: ...
    def suppress_sections(self) -> set[str]: ...


def _section(name: str, content: str) -> ContextSection:
    return ContextSection(name=name, content=content)


class CodingPromptPolicy:
    def base_system_spec(self) -> None:
        return None

    def profile_sections(self, turn_context: object | None) -> list[ContextSection]:
        return []

    def suppress_sections(self) -> set[str]:
        return set()


class ChatPromptPolicy:
    def base_system_spec(self) -> BaseSystemProfile:
        return CHAT_PROFILE_SPEC

    def profile_sections(self, turn_context: object | None) -> list[ContextSection]:
        return []

    def suppress_sections(self) -> set[str]:
        return {"Persona", "Workflow Runtime", "Current Task State"}


class GoalPromptPolicy:
    def base_system_spec(self) -> None:
        return None

    def profile_sections(self, turn_context: object | None) -> list[ContextSection]:
        if turn_context is None:
            return []
        directive = {
            "intake": GOAL_INTAKE_DIRECTIVE,
            "evaluator": GOAL_EVALUATOR_DIRECTIVE,
            "idle": GOAL_IDLE_DIRECTIVE,
        }.get(getattr(turn_context, "goal_phase", ""), "")
        return [_section("Profile Directive", directive)] if directive else []

    def suppress_sections(self) -> set[str]:
        return set()


class LoopPromptPolicy:
    def base_system_spec(self) -> None:
        return None

    def profile_sections(self, turn_context: object | None) -> list[ContextSection]:
        if turn_context is None:
            return []
        parts: list[str] = []
        if getattr(turn_context, "loop_phase", "") == "idle":
            parts.append(LOOP_IDLE_DIRECTIVE)
        system_prompt = str(getattr(getattr(turn_context, "runtime_profile", None), "system_prompt", "") or "").strip()
        if system_prompt:
            parts.append(system_prompt)
        return [_section("Profile Directive", "\n\n".join(parts))] if parts else []

    def suppress_sections(self) -> set[str]:
        return set()


PROMPT_POLICY_REGISTRY: dict[str, type] = {
    "coding": CodingPromptPolicy,
    "chat": ChatPromptPolicy,
    "goal": GoalPromptPolicy,
    "loop": LoopPromptPolicy,
}


def resolve_prompt_policy(policy_id: str) -> PromptPolicy:
    """Instantiate a registered prompt policy by id; YAML may only reference ids."""
    policy_type = PROMPT_POLICY_REGISTRY.get(policy_id.strip().lower())
    if policy_type is None:
        raise ValueError(f"unknown prompt policy: {policy_id}")
    return policy_type()


__all__ = [
    "ChatPromptPolicy", "CodingPromptPolicy", "GoalPromptPolicy", "LoopPromptPolicy",
    "PromptPolicy", "PROMPT_POLICY_REGISTRY", "resolve_prompt_policy",
    "GOAL_EVALUATOR_DIRECTIVE", "GOAL_IDLE_DIRECTIVE", "GOAL_INTAKE_DIRECTIVE",
    "LOOP_IDLE_DIRECTIVE",
]


def revive_prompt_policy(profile_id: str, persisted: object) -> PromptPolicy | None:
    """Rebuild a prompt policy after persistence round-trips.

    ``persisted`` is the serialized type name (RuntimeProfile dumps the policy
    as ``type(policy).__name__``). Rows written before policies were persisted
    carry no value; fall back to the profile id for the bundled four.
    """
    if isinstance(persisted, str) and persisted:
        for policy_type in PROMPT_POLICY_REGISTRY.values():
            if policy_type.__name__ == persisted:
                return policy_type()
        return None
    if profile_id in PROMPT_POLICY_REGISTRY:
        return resolve_prompt_policy(profile_id)
    return None
