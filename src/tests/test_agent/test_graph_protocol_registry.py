"""Graph protocol registry: each runtime profile declares which graph-level protocol tool set it uses."""

from __future__ import annotations

import pytest

from voidx.agent.domain.loop import LOOP_PROFILE
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.infrastructure.langgraph.runtime.graph_protocol import (
    GoalProtocol,
    LoopProtocol,
    TurnToolProtocol,
    resolve_graph_protocol,
)


def test_runtime_profile_defaults_to_turn_protocol() -> None:
    profile = RuntimeProfile(profile_id="coding", revision=1, name="Coding")

    assert profile.protocol == "turn"


def test_loop_profile_declares_loop_protocol() -> None:
    assert LOOP_PROFILE.protocol == "loop"


def test_resolve_turn_protocol_for_default_profile() -> None:
    profile = RuntimeProfile(profile_id="coding", revision=1, name="Coding")

    protocol = resolve_graph_protocol(profile)

    assert isinstance(protocol, TurnToolProtocol)


def test_resolve_loop_protocol_for_loop_profile() -> None:
    protocol = resolve_graph_protocol(LOOP_PROFILE)

    assert isinstance(protocol, LoopProtocol)


def test_resolve_goal_protocol_for_goal_profile() -> None:
    profile = RuntimeProfile(profile_id="goal", revision=1, name="Goal", protocol="goal")

    protocol = resolve_graph_protocol(profile)

    assert isinstance(protocol, GoalProtocol)


def test_unknown_protocol_falls_back_to_turn() -> None:
    profile = RuntimeProfile(profile_id="custom", revision=1, name="Custom", protocol="unknown-x")

    assert isinstance(resolve_graph_protocol(profile), TurnToolProtocol)


def test_resolve_none_profile_falls_back_to_turn() -> None:
    assert isinstance(resolve_graph_protocol(None), TurnToolProtocol)
