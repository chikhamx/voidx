from __future__ import annotations

import pytest
from voidx.agent.adapters.persistence.message_rows import message_from_row
from voidx.agent.adapters.persistence.session_models import MessageRow
from voidx.llm.message_markers import GUIDANCE_MARKER, GUIDANCE_SOURCE_MARKER


def test_legacy_user_guidance_row_hydrates_with_user_source():
    # A legacy user guidance row in DB has role="user", GUIDANCE_MARKER=True, but no GUIDANCE_SOURCE_MARKER
    row = MessageRow(
        id=101,
        session_id="s1",
        thread_id="t1",
        role="user",
        content="Please check line 10",
        additional_kwargs={GUIDANCE_MARKER: True},
    )

    msg = message_from_row(row)
    assert msg is not None
    assert msg.additional_kwargs.get(GUIDANCE_MARKER) is True
    # In-memory hydration should backfill source="user"
    assert msg.additional_kwargs.get(GUIDANCE_SOURCE_MARKER) == "user"


def test_row_with_existing_guidance_source_is_preserved():
    row = MessageRow(
        id=102,
        session_id="s1",
        thread_id="t1",
        role="user",
        content="Guard message",
        additional_kwargs={GUIDANCE_MARKER: True, GUIDANCE_SOURCE_MARKER: "guard"},
    )

    msg = message_from_row(row)
    assert msg is not None
    assert msg.additional_kwargs.get(GUIDANCE_SOURCE_MARKER) == "guard"


def test_non_guidance_or_non_user_row_does_not_backfill_source():
    # Normal user message without guidance marker
    normal_user = MessageRow(
        id=103,
        session_id="s1",
        thread_id="t1",
        role="user",
        content="Regular prompt",
        additional_kwargs={},
    )
    msg1 = message_from_row(normal_user)
    assert msg1 is not None
    assert GUIDANCE_SOURCE_MARKER not in msg1.additional_kwargs

    # Assistant message (even if someone put guidance marker, it's not role="user")
    asst_row = MessageRow(
        id=104,
        session_id="s1",
        thread_id="t1",
        role="assistant",
        content="Ok",
        additional_kwargs={GUIDANCE_MARKER: True},
    )
    msg2 = message_from_row(asst_row)
    assert msg2 is not None
    assert GUIDANCE_SOURCE_MARKER not in msg2.additional_kwargs


def test_invalid_guidance_source_not_migrated_to_user():
    row = MessageRow(
        id=105,
        session_id="s1",
        thread_id="t1",
        role="user",
        content="Unknown source",
        additional_kwargs={GUIDANCE_MARKER: True, GUIDANCE_SOURCE_MARKER: "unrecognized"},
    )
    msg = message_from_row(row)
    assert msg is not None
    # Invalid value is preserved as-is, not promoted to "user"
    assert msg.additional_kwargs.get(GUIDANCE_SOURCE_MARKER) == "unrecognized"
