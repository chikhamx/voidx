"""Pure domain model for the input that starts one real user turn."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ActiveTurnInput(BaseModel):
    """Structured snapshot of the active user turn input."""

    turn_journal_id: str
    user_message_id: int | None = None
    raw_text: str = ""
    semantic_text: str = ""
    display_text: str = ""
    title_text: str = ""
    content: Any = ""
    content_format: str = "text"
    segment_index: int = 0
