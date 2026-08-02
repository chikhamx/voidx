"""Structure tests for the call_llm refactor."""

import importlib


def test_call_llm_helpers_live_in_core_modules():
    for module_name in (
        "voidx.agent.infrastructure.langgraph.runtime.core.loop",
        "voidx.agent.infrastructure.langgraph.runtime.core.turn",
        "voidx.agent.infrastructure.langgraph.runtime.core.context",
    ):
        assert importlib.import_module(module_name)
