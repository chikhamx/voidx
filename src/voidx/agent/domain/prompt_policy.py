"""Profile-scoped prompt injection policy.

A ``PromptPolicy`` declares profile-specific prompt sections via three methods:
- ``base_system_spec()``: override the Base System profile spec (None = keep coding default)
- ``profile_sections(turn_context)``: profile-specific sections inserted after Base System
- ``suppress_sections()``: default section names to suppress from the section list
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from voidx.agent.application.prompts import BaseSystemProfile, CHAT_PROFILE_SPEC

if TYPE_CHECKING:
    from voidx.agent.application.runtime_context import ContextSection


def _section(name: str, content: str):
    from voidx.agent.application.runtime_context import ContextSection

    return ContextSection(name=name, content=content)


class PromptPolicy(Protocol):
    """Declares profile-specific prompt sections."""

    def base_system_spec(self) -> BaseSystemProfile | None:
        """Override the Base System profile spec. None = keep default coding."""
        ...

    def profile_sections(self, turn_context: object | None) -> list[ContextSection]:
        """Profile-specific sections inserted after Base System.

        Can use turn_context to generate phase-dependent directives.
        """
        ...

    def suppress_sections(self) -> set[str]:
        """Default section names to suppress from the section list."""
        ...


class CodingPromptPolicy:
    """Default policy: keep all coding prompt sections unchanged."""

    def base_system_spec(self) -> None:
        return None

    def profile_sections(self, turn_context: object | None) -> list[ContextSection]:
        return []

    def suppress_sections(self) -> set[str]:
        return set()


_CHAT_DIRECTIVE = """\
You are operating in chat profile. This is a constrained conversation session,
not a coding workspace.

Available tools:
- websearch and webfetch for web lookups
- MCP tools registered by the current MCP integration
- read-only filesystem tools (read, find, search, lsp) only when a workspace is bound

Restrictions:
- No shell execution (bash, powershell) and no local writes (write, replace, manage, delete, move)
- No git mutation, agent, or subagent tools
- Tool denials are final; explain the limit to the user and continue without tools when denied

Answer directly using the bound tools. Do not attempt to invoke tools outside the bound set."""


class ChatPromptPolicy:
    """Chat profile: use chat Base System and suppress coding-only sections."""

    def base_system_spec(self) -> BaseSystemProfile:
        return CHAT_PROFILE_SPEC

    def profile_sections(self, turn_context: object | None) -> list[ContextSection]:
        return [_section("Profile Directive", _CHAT_DIRECTIVE)]

    def suppress_sections(self) -> set[str]:
        return {"Persona", "Workflow Runtime", "Current Task State"}


class GoalPromptPolicy:
    """Goal profile: inject phase-dependent directive."""

    def base_system_spec(self) -> None:
        return None

    def profile_sections(self, turn_context: object | None) -> list[ContextSection]:
        if turn_context is None:
            return []
        goal_phase = getattr(turn_context, "goal_phase", "")
        directive = _goal_directive_for_phase(goal_phase)
        if not directive:
            return []
        return [_section("Profile Directive", directive)]

    def suppress_sections(self) -> set[str]:
        return set()


def _goal_directive_for_phase(phase: str) -> str:
    from voidx.agent.domain.goal import (
        GOAL_EVALUATOR_DIRECTIVE,
        GOAL_IDLE_DIRECTIVE,
        GOAL_INTAKE_DIRECTIVE,
    )

    if phase == "intake":
        return GOAL_INTAKE_DIRECTIVE
    if phase == "evaluator":
        return GOAL_EVALUATOR_DIRECTIVE
    if phase == "idle":
        return GOAL_IDLE_DIRECTIVE
    return ""


class LoopPromptPolicy:
    """Loop profile: inject phase directive and loop system prompt."""

    def base_system_spec(self) -> None:
        return None

    def profile_sections(self, turn_context: object | None) -> list[ContextSection]:
        if turn_context is None:
            return []
        parts: list[str] = []
        loop_phase = getattr(turn_context, "loop_phase", "")
        if loop_phase == "idle":
            from voidx.agent.domain.loop import LOOP_IDLE_DIRECTIVE

            parts.append(LOOP_IDLE_DIRECTIVE)
        system_prompt = str(
            getattr(getattr(turn_context, "runtime_profile", None), "system_prompt", "") or ""
        ).strip()
        if system_prompt:
            parts.append(system_prompt)
        if not parts:
            return []
        return [_section("Profile Directive", "\n\n".join(parts))]

    def suppress_sections(self) -> set[str]:
        return set()
