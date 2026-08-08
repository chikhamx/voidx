from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from voidx.agent.application.runtime_context import COMPACTION_GUIDE_MARKER
from voidx.llm.compaction import (
    compaction_summary_messages,
    fallback_summary_with_previous,
)
from voidx.llm.message_markers import GUIDANCE_MARKER, STEP_HINT_MARKER


def test_compaction_summary_messages_keeps_only_selected_semantic_history() -> None:
    selected_head = [
        SystemMessage(content="SYSTEM SENTINEL"),
        HumanMessage(content="VOIDX_RUNTIME_CONTEXT\nstandalone runtime sentinel"),
        HumanMessage(content="real historical request"),
        AIMessage(content="historical answer"),
        ToolMessage(content="historical tool result", tool_call_id="call-1"),
        HumanMessage(
            content="pressure sentinel",
            additional_kwargs={STEP_HINT_MARKER: True, "_voidx_context_pressure": True},
        ),
        HumanMessage(
            content="guidance sentinel",
            additional_kwargs={GUIDANCE_MARKER: True},
        ),
        HumanMessage(content=f"{COMPACTION_GUIDE_MARKER}\ninline guide sentinel"),
        HumanMessage(content="Continue if you have next steps"),
    ]

    filtered = compaction_summary_messages(selected_head)

    assert [message.content for message in filtered] == [
        "real historical request",
        "historical answer",
        "historical tool result",
    ]


def test_compaction_summary_messages_strips_runtime_turn_overlay() -> None:
    filtered = compaction_summary_messages([
        HumanMessage(
            content=(
                "VOIDX_RUNTIME_CONTEXT\ninternal sentinel\n\n"
                "## Task Context\nactual user request"
            )
        )
    ])

    assert len(filtered) == 1
    assert filtered[0].content == "actual user request"


def test_fallback_summary_with_previous_preserves_anchored_history() -> None:
    summary = fallback_summary_with_previous(
        [
            HumanMessage(content="implement the new compaction boundary"),
            AIMessage(content="Updated src/voidx/llm/compaction/service.py"),
        ],
        "## Goal\n- preserve this earlier anchored decision",
    )

    assert "preserve this earlier anchored decision" in summary
    assert "implement the new compaction boundary" in summary
    assert "src/voidx/llm/compaction/service.py" in summary


def test_fallback_summary_with_previous_does_not_reintroduce_filtered_controls() -> None:
    filtered = compaction_summary_messages([
        HumanMessage(content="real request"),
        HumanMessage(
            content="do not leak this guidance",
            additional_kwargs={GUIDANCE_MARKER: True},
        ),
    ])

    summary = fallback_summary_with_previous(filtered, "previous summary")

    assert "previous summary" in summary
    assert "real request" in summary
    assert "do not leak this guidance" not in summary


def test_fallback_summary_with_long_previous_still_keeps_new_history() -> None:
    summary = fallback_summary_with_previous(
        [HumanMessage(content="new history must survive fallback merge")],
        "## Earlier\n" + ("old detail\n" * 5_000),
    )

    assert len(summary) <= 24_000
    assert "new history must survive fallback merge" in summary


def test_compaction_summary_messages_keeps_user_quote_of_legacy_continuation() -> None:
    filtered = compaction_summary_messages([
        HumanMessage(content='The phrase "Continue if you have next steps" is user data.'),
    ])

    assert len(filtered) == 1
