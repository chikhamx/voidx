from voidx.ui.output.agent_display import (
    SUBAGENT_DISPLAY_NAMES,
    agent_display_name,
    subagent_display_name,
)


def test_agent_display_name_returns_raw_identity():
    assert agent_display_name("voidx") == "voidx"
    assert agent_display_name("") == "Agent"
    assert agent_display_name(None) == "Agent"


def test_subagent_display_name_is_stable_for_same_seed():
    first = subagent_display_name("agent_0")
    second = subagent_display_name("agent_0")
    assert first == second
    assert first in SUBAGENT_DISPLAY_NAMES


def test_subagent_display_name_varies_across_seeds():
    names = {subagent_display_name(f"agent_{i}") for i in range(32)}
    assert len(names) >= 2
