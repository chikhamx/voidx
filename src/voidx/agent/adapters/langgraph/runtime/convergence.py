"""Convergence helpers for graph LLM calls."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from voidx.llm.compaction.fallback_summary import dedupe, message_text
from voidx.llm.message_markers import (
    STEP_HINT_MARKER,
    is_compaction_message,
    is_continuation_message,
    is_context_pressure_message,
    is_guidance_message,
    is_step_hint_message,
)


def generate_fallback_summary(state: Mapping[str, Any]) -> str:
    step = int(state.get("step_count", 0) or 0)
    max_steps = int(state.get("max_steps", 0) or 0)
    goal = str(state.get("goal") or "").strip()
    messages = list(state.get("messages", []) or [])
    active_turn_input = state.get("active_turn_input")
    latest_user = _latest_user_text(messages, active_turn_input=active_turn_input)
    tool_results = state.get("tool_results", {}) or {}

    tool_result_count = len(tool_results) + _tool_message_count(messages)
    files = _extract_file_mentions(tool_results)
    files.extend(_extract_file_mentions_from_messages(messages))
    lines: list[str] = []
    if max_steps > 0:
        lines.append(f"Step limit reached: {step}/{max_steps}.")
    if goal:
        lines.append(f"Goal: {goal}")
    elif latest_user:
        lines.append(f"Latest request: {latest_user[:200]}")

    if tool_result_count:
        lines.append(f"Tool results available: {tool_result_count}")
    if files:
        lines.append(f"Relevant paths: {', '.join(dedupe(files)[:5])}")

    lines.append("Pending: continue from the current context or refine the request for the remaining work.")
    return "\n".join(lines)


def _latest_user_text(
    messages: list[BaseMessage],
    active_turn_input: Any | None = None,
) -> str:
    if active_turn_input is not None:
        if isinstance(active_turn_input, dict):
            text = active_turn_input.get("semantic_text") or active_turn_input.get("raw_text")
            if text:
                return str(text)
            content = active_turn_input.get("content")
            if content:
                return str(content)
        else:
            text = (
                getattr(active_turn_input, "semantic_text", None)
                or getattr(active_turn_input, "raw_text", None)
            )
            if text:
                return str(text)
            content = getattr(active_turn_input, "content", None)
            if content:
                return str(content)

    for message in reversed(messages):
        if (
            isinstance(message, HumanMessage)
            and not is_step_hint_message(message)
            and not is_guidance_message(message)
            and not is_compaction_message(message)
            and not is_continuation_message(message)
            and not is_context_pressure_message(message)
        ):
            return message_text(message).strip()
    return ""


def _marked_human_message(content: str) -> HumanMessage:
    return HumanMessage(content=content, additional_kwargs={STEP_HINT_MARKER: True})




def _extract_file_mentions(tool_results: Mapping[Any, Any]) -> list[str]:
    seen: set[str] = set()
    paths: list[str] = []
    for value in tool_results.values():
        text = str(value)
        for raw in text.replace(",", " ").replace(")", " ").replace("(", " ").split():
            token = raw.strip("'\"")
            if "/" not in token and "\\" not in token:
                continue
            token = token.rstrip(".,;:")
            if token and token not in seen:
                seen.add(token)
                paths.append(token[:120])
    return paths


def _extract_file_mentions_from_messages(messages: list[BaseMessage]) -> list[str]:
    pseudo_results = {
        str(index): message_text(message)
        for index, message in enumerate(messages)
        if isinstance(message, ToolMessage)
    }
    return _extract_file_mentions(pseudo_results)


def _tool_message_count(messages: list[BaseMessage]) -> int:
    return sum(1 for message in messages if isinstance(message, ToolMessage))
