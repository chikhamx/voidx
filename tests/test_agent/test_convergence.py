import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from langchain_core.messages import HumanMessage, ToolMessage

from voidx.agent.graph.convergence import (
    STEP_HINT_MARKER,
    build_convergence_messages,
    build_final_convergence_prompt,
    build_step_hint,
    generate_fallback_summary,
    is_step_hint_message,
)
from voidx.llm.message_markers import GUIDANCE_MARKER


def test_build_step_hint_normal_window():
    assert build_step_hint(5, 10, has_tool_budget=True) == ""

    hint = build_step_hint(6, 10, has_tool_budget=True)

    assert "[Step 6/10]" in hint
    assert "4 LLM calls remain" in hint
    assert "Start converging" in hint


def test_build_step_hint_last_tool_step():
    hint = build_step_hint(8, 10, has_tool_budget=True)

    assert "LAST step with tools" in hint


def test_build_step_hint_returns_empty_without_tool_budget():
    assert build_step_hint(9, 10, has_tool_budget=False) == ""


def test_convergence_messages_empty_before_convergence_window():
    messages, forced = build_convergence_messages(
        step=5,
        max_steps=10,
        has_tool_budget=True,
        goal="finish",
    )

    assert messages == []
    assert forced is False


def test_convergence_messages_last_tool_step_hint_is_not_forced():
    messages, forced = build_convergence_messages(
        step=8,
        max_steps=10,
        has_tool_budget=True,
        goal="finish",
    )

    assert forced is False
    assert len(messages) == 1
    assert "LAST step with tools" in messages[0].content
    assert is_step_hint_message(messages[0])


def test_build_final_convergence_prompt():
    prompt = build_final_convergence_prompt(9, 10, "fix the failing tests")

    assert "[Step 9/10] FINAL response step" in prompt
    assert "No tools are available" in prompt
    assert "Original goal: fix the failing tests" in prompt


def test_convergence_messages_are_marked_and_forced_on_final_step():
    messages, forced = build_convergence_messages(
        step=9,
        max_steps=10,
        has_tool_budget=False,
        goal="finish",
    )

    assert forced is True
    assert len(messages) == 1
    assert isinstance(messages[0], HumanMessage)
    assert messages[0].additional_kwargs[STEP_HINT_MARKER] is True
    assert is_step_hint_message(messages[0])


def test_generate_fallback_summary_uses_real_user_and_tool_context():
    hint, _ = build_convergence_messages(
        step=9,
        max_steps=10,
        has_tool_budget=False,
        goal="",
    )
    summary = generate_fallback_summary({
        "messages": [
            HumanMessage(content="Update src/voidx/agent/graph/core.py"),
            *hint,
            ToolMessage(
                content="read /Users/example/project/src/voidx/agent/graph/core.py",
                tool_call_id="tc1",
            ),
        ],
        "tool_results": {"tc2": "pytest failed in tests/test_agent/test_stream_llm.py"},
        "step_count": 9,
        "max_steps": 10,
    })

    assert "Step limit reached: 9/10." in summary
    assert "Latest request: Update src/voidx/agent/graph/core.py" in summary
    assert "Tool results available: 2" in summary
    assert "src/voidx/agent/graph/core.py" in summary
    assert "tests/test_agent/test_stream_llm.py" in summary


def test_generate_fallback_summary_prefers_goal_over_latest_user():
    summary = generate_fallback_summary({
        "messages": [HumanMessage(content="Latest request text")],
        "goal": "Complete the approved implementation",
        "tool_results": {},
        "step_count": 9,
        "max_steps": 10,
    })

    assert "Goal: Complete the approved implementation" in summary
    assert "Latest request text" not in summary


def test_generate_fallback_summary_ignores_guidance_for_latest_user():
    hint, _ = build_convergence_messages(
        step=9,
        max_steps=10,
        has_tool_budget=False,
        goal="",
    )
    summary = generate_fallback_summary({
        "messages": [
            HumanMessage(content="Finish the implementation"),
            HumanMessage(
                content="Use TypeScript",
                additional_kwargs={GUIDANCE_MARKER: True},
            ),
            *hint,
        ],
        "tool_results": {},
        "step_count": 9,
        "max_steps": 10,
    })

    assert "Latest request: Finish the implementation" in summary
    assert "Use TypeScript" not in summary
