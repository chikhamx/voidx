"""Structure tests for the call_llm refactor."""

import importlib


def test_call_llm_helpers_live_in_core_modules():
    for module_name in (
        "voidx.agent.graph.core.loop",
        "voidx.agent.graph.core.turn",
        "voidx.agent.graph.core.context",
    ):
        assert importlib.import_module(module_name)
