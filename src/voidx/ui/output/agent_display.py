"""Display labels for agent roles."""

from __future__ import annotations


_AGENT_DISPLAY_NAMES = {
    "orchestrator": "voidx",
    "agent": "Agent",
    "explore": "Explorer",
    "plan": "Planner",
    "implement": "Implementer",
    "review": "Reviewer",
    "compaction": "Compactor",
    "title": "Title Writer",
}


def agent_display_name(agent: object) -> str:
    raw = str(agent or "").strip()
    if not raw:
        return "Agent"
    key = raw.lower()
    if key in _AGENT_DISPLAY_NAMES:
        return _AGENT_DISPLAY_NAMES[key]
    return " ".join(part[:1].upper() + part[1:] for part in raw.replace("_", " ").split() if part)
