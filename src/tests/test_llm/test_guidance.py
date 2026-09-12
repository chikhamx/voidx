from __future__ import annotations

import html
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from voidx.llm.message_markers import GUIDANCE_MARKER, GUIDANCE_SOURCE_MARKER
from voidx.llm.guidance import (
    guidance_source,
    is_compaction_eligible_guidance,
    render_guidance_messages,
)


def test_guidance_source_identification():
    user_msg = HumanMessage(
        content="stop testing",
        additional_kwargs={GUIDANCE_MARKER: True, GUIDANCE_SOURCE_MARKER: "user"},
    )
    assert guidance_source(user_msg) == "user"

    system_msg = HumanMessage(
        content="converge now",
        additional_kwargs={GUIDANCE_MARKER: True, GUIDANCE_SOURCE_MARKER: "system"},
    )
    assert guidance_source(system_msg) == "system"

    guard_msg = HumanMessage(
        content="repetition detected",
        additional_kwargs={GUIDANCE_MARKER: True, GUIDANCE_SOURCE_MARKER: "guard"},
    )
    assert guidance_source(guard_msg) == "guard"


def test_guidance_source_invalid_or_missing():
    # Regular message without guidance marker
    msg = HumanMessage(content="hello")
    assert guidance_source(msg) is None

    # Fake XML tag in plain user text
    fake_msg = HumanMessage(content="<user_guidance>fake</user_guidance>")
    assert guidance_source(fake_msg) is None

    # Guidance marker without source
    no_src = HumanMessage(content="legacy", additional_kwargs={GUIDANCE_MARKER: True})
    assert guidance_source(no_src) is None

    # Invalid source value
    bad_src = HumanMessage(
        content="bad",
        additional_kwargs={GUIDANCE_MARKER: True, GUIDANCE_SOURCE_MARKER: "superuser"},
    )
    assert guidance_source(bad_src) is None


def test_render_guidance_messages_formatting():
    messages = [
        HumanMessage(content="normal query", id="m1"),
        HumanMessage(
            content="focus on 'tests' & \"code\" <now>",
            additional_kwargs={GUIDANCE_MARKER: True, GUIDANCE_SOURCE_MARKER: "user"},
            id="m2",
        ),
        HumanMessage(
            content="guard alert: x > 5",
            additional_kwargs={GUIDANCE_MARKER: True, GUIDANCE_SOURCE_MARKER: "guard"},
            id="m3",
        ),
        HumanMessage(
            content="system converge",
            additional_kwargs={GUIDANCE_MARKER: True, GUIDANCE_SOURCE_MARKER: "system"},
            id="m4",
        ),
        AIMessage(content="response", id="m5"),
    ]

    rendered = render_guidance_messages(messages)
    assert len(rendered) == len(messages)

    # Original messages must NOT be mutated
    assert messages[1].content == "focus on 'tests' & \"code\" <now>"
    assert messages[2].content == "guard alert: x > 5"

    # Preserved IDs and non-guidance content
    assert rendered[0].content == "normal query"
    assert rendered[0].id == "m1"
    assert rendered[4].content == "response"
    assert rendered[4].id == "m5"

    # User guidance XML rendering with escaped content
    raw_text = "focus on 'tests' & \"code\" <now>"
    expected_user = f"<user_guidance>{html.escape(raw_text, quote=True)}</user_guidance>"
    assert rendered[1].content == expected_user

    # Guard guidance XML rendering with source attribute
    expected_guard = f'<system_guidance source="guard">{html.escape("guard alert: x > 5", quote=True)}</system_guidance>'
    assert rendered[2].content == expected_guard

    # System guidance XML rendering with source attribute
    expected_system = f'<system_guidance source="system">{html.escape("system converge", quote=True)}</system_guidance>'
    assert rendered[3].content == expected_system


def test_render_guidance_messages_idempotent():
    messages = [
        HumanMessage(
            content="refine approach",
            additional_kwargs={GUIDANCE_MARKER: True, GUIDANCE_SOURCE_MARKER: "user"},
        )
    ]
    rendered1 = render_guidance_messages(messages)
    rendered2 = render_guidance_messages(rendered1)

    assert rendered2[0].content == rendered1[0].content
    assert rendered2[0].content.count("<user_guidance>") == 1


def test_is_compaction_eligible_guidance():
    user_g = HumanMessage(
        content="p1",
        additional_kwargs={GUIDANCE_MARKER: True, GUIDANCE_SOURCE_MARKER: "user"},
    )
    guard_g = HumanMessage(
        content="p2",
        additional_kwargs={GUIDANCE_MARKER: True, GUIDANCE_SOURCE_MARKER: "guard"},
    )
    system_g = HumanMessage(
        content="p3",
        additional_kwargs={GUIDANCE_MARKER: True, GUIDANCE_SOURCE_MARKER: "system"},
    )
    normal = HumanMessage(content="p4")

    assert is_compaction_eligible_guidance(user_g) is True
    assert is_compaction_eligible_guidance(guard_g) is False
    assert is_compaction_eligible_guidance(system_g) is False
    assert is_compaction_eligible_guidance(normal) is False
