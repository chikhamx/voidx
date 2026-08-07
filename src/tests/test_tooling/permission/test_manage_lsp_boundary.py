"""Boundary tests for manage and lsp_format external path access intents."""

from __future__ import annotations

from pathlib import Path

import pytest

from voidx.tooling.domain.authorization import PermissionContext
from voidx.tooling.application.authorization import authorize_tool_call, sandbox_precheck_action
from voidx.tooling.policy.permission.rules import classify_tool_call
from voidx.tooling.domain.permission import Action


def _context(workspace: Path) -> PermissionContext:
    return PermissionContext(
        workspace=str(workspace),
        interaction_mode="auto",
        permission_mode="safe",
    )


def test_manage_create_external_produces_write_intent(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "new.txt"

    classified = classify_tool_call({"name": "manage", "args": {"op": "create", "kind": "file", "paths": str(target)}})
    action, reason, intents = sandbox_precheck_action(classified, _context(workspace))

    assert action == "defer"
    assert len(intents) == 1
    assert intents[0].access == "write"
    assert intents[0].is_workspace_path is False


def test_manage_move_external_produces_two_write_intents(tmp_path):
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


def test_manage_move_mixed_internal_external_produces_one_intent(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    src = workspace / "internal.txt"
    dest = external / "external.txt"
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
    external_intents = [i for i in intents if not i.is_workspace_path]
    assert len(external_intents) == 1
    assert external_intents[0].access == "write"


def test_manage_create_internal_produces_no_intents(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "new.txt"

    classified = classify_tool_call({"name": "manage", "args": {"op": "create", "kind": "file", "paths": str(target)}})
    action, reason, intents = sandbox_precheck_action(classified, _context(workspace))

    assert action == "allow"
    assert intents == ()


def test_lsp_format_external_produces_write_intent(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "code.py"
    target.write_text("x = 1\n", encoding="utf-8")

    classified = classify_tool_call({
        "name": "lsp_format",
        "args": {"file_path": str(target), "start_line": 1, "start_character": 0, "end_line": 1, "end_character": 5},
    })
    action, reason, intents = sandbox_precheck_action(classified, _context(workspace))

    assert action == "defer"
    assert len(intents) == 1
    assert intents[0].access == "write"
    assert intents[0].is_workspace_path is False


def test_lsp_format_internal_produces_no_intents(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "code.py"
    target.write_text("x = 1\n", encoding="utf-8")

    classified = classify_tool_call({
        "name": "lsp_format",
        "args": {"file_path": str(target), "start_line": 1, "start_character": 0, "end_line": 1, "end_character": 5},
    })
    action, reason, intents = sandbox_precheck_action(classified, _context(workspace))

    assert action == "allow"
    assert intents == ()


def test_authorize_manage_move_external_shows_only_allow_deny_choices(tmp_path):
    from voidx.agent.infrastructure.langgraph.runtime.permission_flow import _permission_choices
    from voidx.tooling.domain.authorization import PermissionDecision
    from voidx.tooling.domain.grants import AccessIntent

    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    src = external / "src.txt"
    dest = external / "dest.txt"
    src.write_text("s", encoding="utf-8")

    decision = authorize_tool_call(
        {
            "name": "manage",
            "args": {
                "op": "move",
                "kind": "file",
                "moves": [{"src": str(src), "dest": str(dest), "overwrite": False}],
            },
        },
        _context(workspace),
    )

    assert decision.action == Action.ASK
    assert len(decision.access_intents) == 2
    choices = _permission_choices([decision])
    values = [c[1] for c in choices]
    assert "allow" in values
    assert "deny" in values
    assert "session_file" not in values


def test_lsp_external_produces_read_intent(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    target = external / "code.py"
    target.write_text("x = 1\n", encoding="utf-8")

    classified = classify_tool_call({
        "name": "lsp",
        "args": {"file_path": str(target), "operation": "diagnostics", "line": 1, "character": 0},
    })
    action, reason, intents = sandbox_precheck_action(classified, _context(workspace))

    assert action == "defer"
    assert len(intents) == 1
    assert intents[0].access == "read"
    assert intents[0].is_workspace_path is False


def test_lsp_internal_produces_no_intents(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "code.py"
    target.write_text("x = 1\n", encoding="utf-8")

    classified = classify_tool_call({
        "name": "lsp",
        "args": {"file_path": str(target), "operation": "diagnostics", "line": 1, "character": 0},
    })
    action, reason, intents = sandbox_precheck_action(classified, _context(workspace))

    assert action == "allow"
    assert intents == ()
