"""Tests for child-agent run control."""

from voidx.tools.agent_control import AgentControlInput, AgentControlTool, _WAIT_TIMEOUTS
from voidx.tools.base import model_to_json_schema


def test_agent_control_schema_and_timeout_mapping():
    schema = model_to_json_schema(AgentControlInput)
    assert set(schema["properties"]) == {"action", "run_id", "wait"}
    assert set(schema["properties"]["action"]["enum"]) == {"wait", "cancel"}
    assert set(schema["properties"]["wait"]["enum"]) == {"brief", "extended", "until_complete"}
    assert _WAIT_TIMEOUTS == {"brief": 5.0, "extended": 30.0, "until_complete": 0.0}


def test_agent_control_required_fields_are_explicit():
    assert set(AgentControlInput.model_json_schema()["required"]) >= {"action", "run_id"}


def test_agent_control_cancel_ignores_wait_strategy():
    inp = AgentControlInput(action="cancel", run_id="run_123", wait="extended")
    assert inp.action == "cancel"
    assert inp.run_id == "run_123"
    assert inp.wait == "extended"
