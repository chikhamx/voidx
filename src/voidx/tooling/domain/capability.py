"""Stable capability classes for registered tools."""

from __future__ import annotations

from enum import StrEnum


class ToolCapability(StrEnum):
    HITL_INTERACTION = "hitl_interaction"
    EXECUTION_GATED = "execution_gated"
    READ_ONLY = "read_only"
    ORCHESTRATION = "orchestration"
    EXTERNAL = "external"
