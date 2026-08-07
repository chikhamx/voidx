"""Tests for permission_flow _permission_choices with access intents."""

from __future__ import annotations

from pathlib import Path

from voidx.agent.infrastructure.langgraph.runtime.permission_flow import _permission_choices
from voidx.tooling.domain.authorization import PermissionContext, PermissionDecision
from voidx.tooling.domain.grants import AccessIntent
from voidx.tooling.policy.permission.rules import classify_tool_call
from voidx.tooling.domain.permission import Action


def _intent(path: str, access: str = "write", object_type: str = "file") -> AccessIntent:
    return AccessIntent(
        requested_path=path,
        normalized_path=Path(path),
        access=access,
        object_type=object_type,
        is_workspace_path=False,
        grant_matched=False,
    )


def _decision(name: str = "write", args: dict | None = None, intents: tuple[AccessIntent, ...] = ()) -> PermissionDecision:
    classified = classify_tool_call({"name": name, "args": args or {"file_path": "/external/x.txt"}})
    return PermissionDecision(
        action=Action.ASK,
        tool_call=classified.tool_call,
        name=classified.name,
        args=classified.args,
        pattern=classified.pattern,
        capability=classified.capability,
        source="sandbox",
        access_intents=intents,
    )


def test_single_external_file_intent_shows_friendly_grant_options():
    decision = _decision(intents=(_intent("/external/x.txt"),))
    choices = _permission_choices([decision])
    values = [c[1] for c in choices]
    assert "allow" in values
    assert "session_file" in values
    assert "session_dir" in values
    assert "persistent_file" in values
    assert "persistent_dir" in values
    assert "deny" in values


def test_single_external_dir_intent_shows_friendly_grant_options():
    decision = _decision(intents=(_intent("/external/dir", object_type="dir"),))
    choices = _permission_choices([decision])
    values = [c[1] for c in choices]
    assert "session_file" in values
    assert "session_dir" in values
    assert "persistent_file" in values
    assert "persistent_dir" in values


def test_multiple_external_intents_show_only_allow_once_and_deny():
    decision = _decision(
        name="manage",
        args={"op": "move", "moves": [{"src": "/external/a.txt", "dest": "/external/b.txt"}]},
        intents=(_intent("/external/a.txt"), _intent("/external/b.txt")),
    )
    choices = _permission_choices([decision])
    values = [c[1] for c in choices]
    assert "allow" in values
    assert "deny" in values
    assert "session_file" not in values
    assert "session_dir" not in values
    assert "persistent_file" not in values
    assert "persistent_dir" not in values


def test_no_external_intents_keeps_legacy_choices():
    decision = _decision(intents=())
    choices = _permission_choices([decision])
    values = [c[1] for c in choices]
    assert "y" in values
    assert "n" in values


def test_blocked_ack_shows_do_not_run():
    decision = PermissionDecision(
        action=Action.BLOCKED_ACK,
        tool_call={"name": "bash", "args": {"command": "rm -rf /"}},
        name="bash",
        args={"command": "rm -rf /"},
        pattern="rm -rf /",
        capability=None,
        source="sandbox",
    )
    choices = _permission_choices([decision])
    assert choices == [("Do not run", "n", "This command is blocked")]
