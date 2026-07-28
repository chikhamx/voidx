"""Permission system — aligned with opencode/Claude Code architecture.

Imports are lazy so that leaf submodules (e.g. ``voidx.permission.grants``)
can be imported from lower layers without pulling in the engine chain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from voidx.permission.schema import Action, Rule, Ruleset
    from voidx.permission.wildcard import match as wildcard_match
    from voidx.permission.evaluate import evaluate, from_config, merge
    from voidx.permission.service import PermissionService
    from voidx.permission.presets import PermissionMode, ModeDecision, resolve_mode_decision
    from voidx.permission.risk import ApprovalScope, RiskAssessment, RiskLevel, RiskTag
    from voidx.permission.sandbox import check_sandbox_filepath, check_sandbox_bash
    from voidx.permission.engine import (
        PermissionCapability,
        PermissionContext,
        PermissionDecision,
        authorize_tool_call,
        classify_tool_call,
    )

__all__ = [
    "Action", "Rule", "Ruleset",
    "wildcard_match",
    "evaluate", "from_config", "merge",
    "PermissionService",
    "PermissionMode",
    "ModeDecision",
    "resolve_mode_decision",
    "ApprovalScope",
    "RiskAssessment",
    "RiskLevel",
    "RiskTag",
    "check_sandbox_filepath",
    "check_sandbox_bash",
    "PermissionCapability",
    "PermissionContext",
    "PermissionDecision",
    "authorize_tool_call",
    "classify_tool_call",
]

_MODULE_FOR = {
    "Action": "schema", "Rule": "schema", "Ruleset": "schema",
    "wildcard_match": "wildcard",
    "evaluate": "evaluate", "from_config": "evaluate", "merge": "evaluate",
    "PermissionService": "service",
    "PermissionMode": "presets", "ModeDecision": "presets", "resolve_mode_decision": "presets",
    "ApprovalScope": "risk", "RiskAssessment": "risk", "RiskLevel": "risk", "RiskTag": "risk",
    "check_sandbox_filepath": "sandbox", "check_sandbox_bash": "sandbox",
    "PermissionCapability": "engine", "PermissionContext": "engine",
    "PermissionDecision": "engine", "authorize_tool_call": "engine", "classify_tool_call": "engine",
}


def __getattr__(name: str):
    module = _MODULE_FOR.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(f"voidx.permission.{module}"), name)
