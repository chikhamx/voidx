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


def test_compaction_summary_messages_strips_xml_task_state_overlay() -> None:
    filtered = compaction_summary_messages([
        HumanMessage(
            content=(
                "<current_task_state>\n- Current persona: voidx\n</current_task_state>\n\nactual user request"
            )
        )
    ])

    assert len(filtered) == 1
    assert filtered[0].content == "actual user request"


def test_compaction_summary_messages_strips_multimodal_task_state_overlay() -> None:
    filtered = compaction_summary_messages([
        HumanMessage(
            content=[
                {"type": "text", "text": "<current_task_state>\n- Current persona: voidx\n</current_task_state>\n\nactual user request"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,123"}},
            ]
        )
    ])

    assert len(filtered) == 1
    assert filtered[0].content[0]["text"] == "actual user request"
    assert filtered[0].content[1]["type"] == "image_url"


def test_compaction_summary_messages_does_not_drop_user_custom_xml_tags() -> None:
    filtered = compaction_summary_messages([
        HumanMessage(content="<code>print('hello')</code>"),
        HumanMessage(content="<sql>SELECT * FROM users;</sql>"),
    ])

    assert len(filtered) == 2
    assert filtered[0].content == "<code>print('hello')</code>"
    assert filtered[1].content == "<sql>SELECT * FROM users;</sql>"


def test_compaction_summary_messages_drops_standalone_task_state_overlay() -> None:
    filtered = compaction_summary_messages([
        HumanMessage(content="<current_task_state>\n- Current persona: voidx\n</current_task_state>"),
        HumanMessage(content="valid user question"),
        HumanMessage(content=[
            {"type": "text", "text": "<current_task_state>\n- Current persona: voidx\n</current_task_state>"},
        ]),
    ])

    assert len(filtered) == 1
    assert filtered[0].content == "valid user question"


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
def test_compaction_summary_messages_retains_user_guidance_and_filters_system_guard() -> None:
    from voidx.llm.message_markers import GUIDANCE_SOURCE_MARKER

    messages = [
        HumanMessage(content="initial request"),
        HumanMessage(
            content="user guidance: do not use regex",
            additional_kwargs={GUIDANCE_MARKER: True, GUIDANCE_SOURCE_MARKER: "user"},
        ),
        HumanMessage(
            content="guard guidance: repetition alert",
            additional_kwargs={GUIDANCE_MARKER: True, GUIDANCE_SOURCE_MARKER: "guard"},
        ),
        HumanMessage(
            content="system guidance: converge now",
            additional_kwargs={GUIDANCE_MARKER: True, GUIDANCE_SOURCE_MARKER: "system"},
        ),
        HumanMessage(
            content="legacy guidance without source",
            additional_kwargs={GUIDANCE_MARKER: True},
        ),
        HumanMessage(
            content="quoted text with <user_guidance>tag</user_guidance>",
        ),
    ]

    filtered = compaction_summary_messages(messages)
    contents = [m.content for m in filtered]

    assert "initial request" in contents
    assert "user guidance: do not use regex" in contents
    assert "guard guidance: repetition alert" not in contents
    assert "system guidance: converge now" not in contents
    assert "legacy guidance without source" not in contents
    assert "quoted text with <user_guidance>tag</user_guidance>" in contents
