"""Shared helpers for LangChain message status normalization."""

from __future__ import annotations

from typing import Literal


MessageStatus = Literal["success", "error"]


def message_status(value: object) -> MessageStatus:
    return "error" if value == "error" else "success"
