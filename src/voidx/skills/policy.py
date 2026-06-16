"""Compatibility aliases for workflow policy.

Workflow policy now lives in :mod:`voidx.workflow.policy`.
"""

from __future__ import annotations

from voidx.workflow.policy import (
    WORKFLOW_PRIORITY,
    WORKFLOW_TRANSITIONS,
    workflow_denied_tools,
    workflow_edges,
    workflow_exit_summaries,
    workflow_gate,
    workflow_sort_key,
    workflow_tools,
    workflow_transitions,
)

WORKFLOW_SKILL_PRIORITY = WORKFLOW_PRIORITY
WORKFLOW_SKILL_TRANSITIONS = WORKFLOW_TRANSITIONS
workflow_skill_denied_tools = workflow_denied_tools
workflow_skill_edges = workflow_edges
workflow_skill_exit_summaries = workflow_exit_summaries
workflow_skill_gate = workflow_gate
workflow_skill_sort_key = workflow_sort_key
workflow_skill_tools = workflow_tools
workflow_skill_transitions = workflow_transitions

__all__ = [
    "WORKFLOW_SKILL_PRIORITY",
    "WORKFLOW_SKILL_TRANSITIONS",
    "workflow_skill_denied_tools",
    "workflow_skill_edges",
    "workflow_skill_exit_summaries",
    "workflow_skill_gate",
    "workflow_skill_sort_key",
    "workflow_skill_tools",
    "workflow_skill_transitions",
]
