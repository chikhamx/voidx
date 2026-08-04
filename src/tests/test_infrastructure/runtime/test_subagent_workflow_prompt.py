"""Tests for isolated child workflow prompts."""

from voidx.agent.application.prompts import child_workflow_runtime


def test_child_workflow_prompt_contains_only_mode_route_nodes():
    review = child_workflow_runtime("review").render()
    debug = child_workflow_runtime("debug").render()
    implement = child_workflow_runtime("implement").render()

    assert "review" in review
    assert "brainstorm" not in review
    assert "\n## design\n" not in review
    assert "\n## plan\n" not in review
    assert "debug" in debug
    assert "brainstorm" not in debug
    assert "tdd" in implement
    assert "verify" in implement
    assert "\n## brainstorm\n" not in implement
    assert "\n## design\n" not in implement
