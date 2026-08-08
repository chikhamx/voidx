"""Structure tests for the call_llm refactor."""

import importlib


def test_call_llm_helpers_live_in_core_modules():
    for module_name in (
        "voidx.agent.adapters.langgraph.runtime.core.loop",
        "voidx.agent.adapters.langgraph.runtime.core.turn",
        "voidx.agent.adapters.langgraph.runtime.core.context",
    ):
        assert importlib.import_module(module_name)
