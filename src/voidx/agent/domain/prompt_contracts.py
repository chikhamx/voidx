"""Domain-neutral prompt contracts shared by profiles and application assembly."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BaseSystemProfile(BaseModel):
    """Profile-scoped base system prompt assembly spec."""

    identity: str
    style_names: list[str] = Field(default_factory=list)
    global_section_names: dict[str, list[str]] = Field(default_factory=dict)


class ContextSection(BaseModel):
    name: str
    content: str


CHAT_PROFILE_SPEC = BaseSystemProfile(
    identity="You are voidx, a conversational assistant.",
    style_names=[
        "language",
        "tone",
        "concise",
        "progress_preamble",
        "summarize_results",
        "uncertainty",
    ],
    global_section_names={
        "Verification Rules": ["fresh_verification"],
        "Collaboration Rules": ["min_questions", "follow_requests"],
    },
)


__all__ = ["BaseSystemProfile", "CHAT_PROFILE_SPEC", "ContextSection"]
