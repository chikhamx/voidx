"""Profile-scoped prompt injection policy.

A ``PromptPolicy`` decides which prompt sections a runtime profile overrides.
``None`` means "keep the default coding injection"; an empty string means
"suppress this section". ``profile_directive`` is an optional profile-specific
section inserted after Base System.
"""

from __future__ import annotations

from typing import Protocol

from voidx.agent.prompts import BaseSystemProfile, CHAT_PROFILE_SPEC


class PromptPolicy(Protocol):
    """Controls which prompt sections a profile overrides."""

    @property
    def base_system_spec(self) -> BaseSystemProfile | None:
        """Override the Base System profile spec. None = keep default coding."""
        ...

    @property
    def persona_prompt(self) -> str | None:
        """Override the persona section. None = keep default, "" = suppress."""
        ...

    @property
    def workflow_runtime(self) -> str | None:
        """Override the workflow runtime section. None = keep, "" = suppress."""
        ...

    @property
    def task_state_section(self) -> str | None:
        """Override the Current Task State section. None = keep, "" = suppress."""
        ...

    @property
    def profile_directive(self) -> str | None:
        """Optional profile-specific directive inserted after Base System."""
        ...


class CodingPromptPolicy:
    """Default policy: keep all coding prompt sections unchanged."""

    @property
    def base_system_spec(self) -> BaseSystemProfile | None:
        return None

    @property
    def persona_prompt(self) -> str | None:
        return None

    @property
    def workflow_runtime(self) -> str | None:
        return None

    @property
    def task_state_section(self) -> str | None:
        return None

    @property
    def profile_directive(self) -> str | None:
        return None


_CHAT_DIRECTIVE = """\
You are operating in chat profile. This is a constrained conversation session,
not a coding workspace.

Available tools:
- websearch and webfetch for web lookups
- MCP tools registered by the current MCP integration
- read-only filesystem tools (read, glob, grep, lsp) only when a workspace is bound

Restrictions:
- No shell execution (bash, powershell) and no local writes (write, replace, manage, delete, move)
- No git mutation, agent, or subagent tools
- Tool denials are final; explain the limit to the user and continue without tools when denied

Answer directly using the bound tools. Do not attempt to invoke tools outside the bound set."""


class ChatPromptPolicy:
    """Chat profile: use chat Base System and suppress coding-only sections."""

    @property
    def base_system_spec(self) -> BaseSystemProfile | None:
        return CHAT_PROFILE_SPEC

    @property
    def persona_prompt(self) -> str | None:
        return ""

    @property
    def workflow_runtime(self) -> str | None:
        return ""

    @property
    def task_state_section(self) -> str | None:
        return ""

    @property
    def profile_directive(self) -> str | None:
        return _CHAT_DIRECTIVE
