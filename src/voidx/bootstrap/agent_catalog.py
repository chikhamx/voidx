"""Agent tool catalog — static enumeration for configuration UIs.

Composition of builtin tooling plugins and agent orchestration plugins;
both are adapter-level factories, so this lives in the composition root.
"""

from __future__ import annotations

from voidx.agent.adapters.tools.plugins import build_agent_plugins
from voidx.tooling.builtin.plugins import build_builtin_plugins


def tool_catalog() -> list[dict[str, str]]:
    seen: dict[str, str] = {}
    for plugin in (*build_builtin_plugins(), *build_agent_plugins()):
        if plugin.id not in seen:
            seen[plugin.id] = plugin.description or ""
    return [{"id": tool_id, "description": description} for tool_id, description in seen.items()]


__all__ = ["tool_catalog"]
