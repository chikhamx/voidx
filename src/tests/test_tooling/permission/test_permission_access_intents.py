"""Tests for access intent propagation from sandbox_precheck_action."""

from __future__ import annotations

from pathlib import Path

import pytest

from voidx.tooling.domain.authorization import PermissionContext
from voidx.tooling.application.authorization import (
    authorize_tool_call,
    sandbox_denial_reason,
    sandbox_precheck_action,
)
from voidx.tooling.policy.permission.rules import classify_tool_call
from voidx.tooling.domain.permission import Action


def _context(workspace: Path, **overrides) -> PermissionContext:
    base = dict(
        workspace=str(workspace),
        interaction_mode="auto",
        permission_mode="safe",
    )
    base.update(overrides)
    return PermissionContext(**base)


def test_sandbox_precheck_returns_write_intent_for_external_write(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "out.txt"

    classified = classify_tool_call({"name": "write", "args": {"file_path": str(target), "op": "write", "new_string": "x"}})
    action, reason, intents = sandbox_precheck_action(classified, _context(workspace))

    assert action == "defer"
    assert len(intents) == 1
    intent = intents[0]
    assert intent.access == "write"
    assert intent.is_workspace_path is False


def test_sandbox_precheck_returns_read_intent_for_external_read(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "in.txt"
    target.write_text("data", encoding="utf-8")

    classified = classify_tool_call({"name": "read", "args": {"file_path": str(target)}})
    action, reason, intents = sandbox_precheck_action(classified, _context(workspace))

    assert action == "defer"
    assert len(intents) == 1
    intent = intents[0]
    assert intent.access == "read"
    assert intent.is_workspace_path is False


def test_sandbox_precheck_returns_multiple_intents_for_manage_move(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    src = external / "src.txt"
    dest = external / "dest.txt"
    src.write_text("s", encoding="utf-8")

    classified = classify_tool_call({
        "name": "manage",
        "args": {
            "op": "move",
            "kind": "file",
            "moves": [{"src": str(src), "dest": str(dest), "overwrite": False}],
        },
    })
    action, reason, intents = sandbox_precheck_action(classified, _context(workspace))

    assert action == "defer"
    assert len(intents) == 2
    assert all(i.access == "write" for i in intents)
    assert all(not i.is_workspace_path for i in intents)


def test_sandbox_precheck_returns_no_intents_for_workspace_internal(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "internal.txt"

    classified = classify_tool_call({"name": "write", "args": {"file_path": str(target), "op": "write", "new_string": "x"}})
    action, reason, intents = sandbox_precheck_action(classified, _context(workspace))

    assert action == "allow"
    assert intents == ()


def test_authorize_tool_call_decision_carries_access_intents(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "out.txt"

    decision = authorize_tool_call(
        {"name": "write", "args": {"file_path": str(target), "op": "write", "new_string": "x"}},
        _context(workspace),
    )

    assert decision.action == Action.ASK
    assert len(decision.access_intents) == 1
    assert decision.access_intents[0].access == "write"


def test_sandbox_denial_reason_handles_three_tuple(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = str(workspace / "internal.txt")

    classified = classify_tool_call({"name": "write", "args": {"file_path": target, "op": "write", "new_string": "x"}})
    reason = sandbox_denial_reason(classified, _context(workspace))

    assert reason is None
