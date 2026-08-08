import sys
from pathlib import Path


from langchain_core.messages import HumanMessage, ToolMessage

from voidx.agent.adapters.langgraph.runtime.convergence import (
    generate_fallback_summary,
    is_step_hint_message,
)
from voidx.llm.message_markers import GUIDANCE_MARKER


def test_generate_fallback_summary_uses_real_user_and_tool_context():
    summary = generate_fallback_summary({
        "messages": [
            HumanMessage(content="Update src/voidx/agent/graph/core.py"),
            ToolMessage(
                content="read /Users/example/project/src/voidx/agent/graph/core.py",
                tool_call_id="tc1",
            ),
        ],
        "tool_results": {"tc2": "pytest failed in tests/test_agent/test_stream_llm.py"},
        "step_count": 9,
        "max_steps": 0,
    })

    assert "Step limit reached" not in summary
    assert "Latest request: Update src/voidx/agent/graph/core.py" in summary
    assert "Tool results available: 2" in summary
    assert "src/voidx/agent/graph/core.py" in summary
    assert "tests/test_agent/test_stream_llm.py" in summary


def test_generate_fallback_summary_includes_step_limit_when_max_steps_set():
    summary = generate_fallback_summary({
        "messages": [],
        "tool_results": {},
        "step_count": 9,
        "max_steps": 10,
    })

    assert "Step limit reached: 9/10." in summary


def test_generate_fallback_summary_prefers_goal_over_latest_user():
    summary = generate_fallback_summary({
        "messages": [HumanMessage(content="Latest request text")],
        "goal": "Complete the approved implementation",
        "tool_results": {},
        "step_count": 9,
        "max_steps": 0,
    })

    assert "Goal: Complete the approved implementation" in summary
    assert "Latest request text" not in summary


def test_generate_fallback_summary_ignores_guidance_for_latest_user():
    summary = generate_fallback_summary({
        "messages": [
            HumanMessage(content="Finish the implementation"),
            HumanMessage(
                content="Use TypeScript",
                additional_kwargs={GUIDANCE_MARKER: True},
            ),
        ],
        "tool_results": {},
        "step_count": 9,
        "max_steps": 0,
    })

    assert "Latest request: Finish the implementation" in summary
    assert "Use TypeScript" not in summary
