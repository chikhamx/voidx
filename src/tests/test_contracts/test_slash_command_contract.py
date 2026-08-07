from __future__ import annotations

from dataclasses import asdict

from voidx.agent.slash.registry import SLASH_COMMANDS

from .snapshot import assert_snapshot


def test_slash_command_contract() -> None:
    assert_snapshot("slash_commands.json", [asdict(command) for command in SLASH_COMMANDS])
