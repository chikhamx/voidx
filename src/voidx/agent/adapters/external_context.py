"""Bindings for external tool context owned outside the agent core."""

from __future__ import annotations

from typing import Any

from voidx.mcp.context import strip_mcp_tool_context
from voidx.skills.context import strip_skill_tool_context


def strip_external_tool_context(content: Any) -> Any:
    return strip_mcp_tool_context(strip_skill_tool_context(content))
