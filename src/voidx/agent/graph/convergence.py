"""Step-limit convergence helpers for graph and subagent LLM calls."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from voidx.llm.message_markers import (
    STEP_HINT_MARKER,
    is_guidance_message,
    is_step_hint_message,
)


def build_step_hint(
    step: int,
    max_steps: int,
    *,
    has_tool_budget: bool,
) -> str:
    if step <= 0 or max_steps <= 0:
        return ""
    if not has_tool_budget:
        return ""
    remaining_calls = max_steps - step
    if remaining_calls > 4:
        return ""
    if step == max_steps - 2:
        return (
            f"[Step {step}/{max_steps}] This is the LAST step with tools. "
            "Use tools only for final verification or essential missing facts, then converge."
        )
    return (
        f"[Step {step}/{max_steps}] {remaining_calls} LLM calls remain. "
        "Start converging; avoid broad new exploration."
    )


def build_final_convergence_prompt(step: int, max_steps: int, goal: str) -> str:
    return (
        f"[Step {step}/{max_steps}] FINAL response step. No tools are available.\n\n"
        "Provide the best final answer now:\n"
        "1. Result: what was accomplished or learned\n"
        "2. Pending: what remains uncertain, blocked, or needs follow-up\n"
        "3. Resume point: if work is incomplete, state exactly where the next attempt should start (file, line, command)\n\n"
        f"Original goal: {goal or '(unknown)'}\n"
        "Do not describe tool calls or request more tool use."
    )


def build_convergence_messages(
    *,
    step: int,
    max_steps: int,
    has_tool_budget: bool,
    goal: str,
) -> tuple[list[HumanMessage], bool]:
    messages: list[HumanMessage] = []
    hint = build_step_hint(
        step,
        max_steps,
        has_tool_budget=has_tool_budget,
    )
    if hint:
        messages.append(_marked_human_message(hint))

    forced = step > 0 and not has_tool_budget and step <= max_steps
    if forced:
        messages.append(_marked_human_message(build_final_convergence_prompt(step, max_steps, goal)))

    return messages, forced


def generate_fallback_summary(state: Mapping[str, Any]) -> str:
    step = int(state.get("step_count", 0) or 0)
    max_steps = int(state.get("max_steps", 0) or 0)
    goal = str(state.get("goal") or "").strip()
    messages = list(state.get("messages", []) or [])
    latest_user = _latest_user_text(messages)
    tool_results = state.get("tool_results", {}) or {}

    tool_result_count = len(tool_results) + _tool_message_count(messages)
    files = _extract_file_mentions(tool_results)
    files.extend(_extract_file_mentions_from_messages(messages))
    lines = [f"Step limit reached: {step}/{max_steps}."]
    if goal:
        lines.append(f"Goal: {goal}")
    elif latest_user:
        lines.append(f"Latest request: {latest_user[:200]}")

    if tool_result_count:
        lines.append(f"Tool results available: {tool_result_count}")
    if files:
        lines.append(f"Relevant paths: {', '.join(_dedupe(files)[:5])}")

    lines.append("Pending: continue from the current context or refine the request for the remaining work.")
    return "\n".join(lines)


def _marked_human_message(content: str) -> HumanMessage:
    return HumanMessage(content=content, additional_kwargs={STEP_HINT_MARKER: True})


def _latest_user_text(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if (
            isinstance(message, HumanMessage)
            and not is_step_hint_message(message)
            and not is_guidance_message(message)
        ):
            return _message_text(message).strip()
    return ""


def _extract_file_mentions(tool_results: Mapping[Any, Any]) -> list[str]:
    seen: set[str] = set()
    paths: list[str] = []
    for value in tool_results.values():
        text = str(value)
        for raw in text.replace(",", " ").replace(")", " ").replace("(", " ").split():
            token = raw.strip("`'\"")
            if "/" not in token and "\\" not in token:
                continue
            token = token.rstrip(":;")
            if token and token not in seen:
                seen.add(token)
                paths.append(token[:120])
    return paths


def _extract_file_mentions_from_messages(messages: list[BaseMessage]) -> list[str]:
    pseudo_results = {
        str(index): _message_text(message)
        for index, message in enumerate(messages)
        if isinstance(message, ToolMessage)
    }
    return _extract_file_mentions(pseudo_results)


def _tool_message_count(messages: list[BaseMessage]) -> int:
    return sum(1 for message in messages if isinstance(message, ToolMessage))


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _message_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part)
    if isinstance(message, AIMessage | ToolMessage):
        return str(content)
    return str(content)
