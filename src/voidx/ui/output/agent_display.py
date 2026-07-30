"""Display labels for agent identities."""

from __future__ import annotations


def agent_display_name(agent: object) -> str:
    raw = str(agent or "").strip()
    return raw or "Agent"
