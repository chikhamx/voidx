"""Display labels for agent identities."""

from __future__ import annotations

from voidx.agent.domain.subagent_display import SUBAGENT_DISPLAY_NAMES, subagent_display_name


def agent_display_name(agent: object) -> str:
    raw = str(agent or "").strip()
    return raw or "Agent"

