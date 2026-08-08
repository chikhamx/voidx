"""Synthetic message markers shared by LLM context services."""

from __future__ import annotations

STEP_HINT_MARKER = "_voidx_step_hint"
GUIDANCE_MARKER = "_voidx_guidance"
CONTEXT_PRESSURE_MARKER = "_voidx_context_pressure"


def is_step_hint_message(message: object) -> bool:
    return bool(getattr(message, "additional_kwargs", {}).get(STEP_HINT_MARKER))


def is_guidance_message(message: object) -> bool:
    return bool(getattr(message, "additional_kwargs", {}).get(GUIDANCE_MARKER))


def is_context_pressure_message(message: object) -> bool:
    return bool(getattr(message, "additional_kwargs", {}).get(CONTEXT_PRESSURE_MARKER))
