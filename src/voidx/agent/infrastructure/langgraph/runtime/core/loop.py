from __future__ import annotations

from voidx.agent.infrastructure.ui_events import AssistantStreamCommitted, AssistantStreamUpdated, StatusFinished, StatusUpdated

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import AIMessage

from .helpers import LLMErrorKind, _clean_error_message, _llm_retry_delay, _llm_retry_sleep_delay


@dataclass
class LlmLoopState:
    context_tokens: int
    failed_attempts: int = 0
    timeout_retry_attempts: int = 0
    overflow_compaction_attempts: int = 0
    malformed_tool_call_attempts: int = 0
    retry_status_active: bool = False
    pending_provisional: AIMessage | None = None
    missing_turn_count: int = 0
    protocol_repairs: int = 0
    invalid_turn_repairs: int = 0
    turn_prompt_active: bool = False
    start_prompt_injected: bool = False
    terminal_msg: AIMessage | None = None
    terminal_msg_visible: bool = True
    pending_provisional_visible: bool = True


@dataclass
class LlmRetryResult:
    action: Literal["retry", "fail", "overflow"]
    failure_text: str = ""


async def handle_llm_exception(
    *,
    ui: Any,
    loop: LlmLoopState,
    error: Exception,
    kind: LLMErrorKind,
    max_retries: int,
    timeout_max_retries: int,
) -> LlmRetryResult:
    if kind == LLMErrorKind.CONTEXT_OVERFLOW and loop.overflow_compaction_attempts < 1:
        loop.overflow_compaction_attempts += 1
        return LlmRetryResult("overflow")

    if kind == LLMErrorKind.NON_RETRYABLE:
        failure_text = f"LLM call failed (non-retryable): {_clean_error_message(error)}"
        if ui.via_events():
            await ui.events.emit(StatusUpdated(
                status_id="llm:retry",
                label="Failed",
                detail=failure_text,
            ))
            await ui.events.emit(StatusFinished(status_id="llm:retry"))
            await ui.events.emit(AssistantStreamUpdated(text=failure_text))
            await ui.events.emit(AssistantStreamCommitted())
        else:
            ui.ui.error(failure_text)
        return LlmRetryResult("fail", failure_text)

    if kind == LLMErrorKind.TIMEOUT:
        loop.timeout_retry_attempts += 1
        if loop.timeout_retry_attempts > timeout_max_retries:
            failure_text = f"LLM call failed after {loop.timeout_retry_attempts} timeout(s): {_clean_error_message(error)}"
            if loop.retry_status_active and ui.via_events():
                await ui.events.emit(StatusFinished(status_id="llm:retry"))
            if ui.via_events():
                await ui.events.emit(AssistantStreamUpdated(text=failure_text))
                await ui.events.emit(AssistantStreamCommitted())
            else:
                ui.ui.error(failure_text)
            return LlmRetryResult("fail", failure_text)

    if loop.failed_attempts < max_retries:
        loop.failed_attempts += 1
        delay = _llm_retry_delay(loop.failed_attempts)
        delay_str = str(int(delay)) if delay == int(delay) else str(delay)
        retry_detail = f"retrying in {delay_str}s: {_clean_error_message(error)}"
        if ui.via_events():
            loop.retry_status_active = True
            await ui.events.emit(StatusUpdated(
                status_id="llm:retry",
                label="Retrying",
                detail=retry_detail,
            ))
        else:
            ui.ui.print(f"[dim]Retrying ({retry_detail})[/dim]")
        await asyncio.sleep(_llm_retry_sleep_delay(delay))
        return LlmRetryResult("retry")

    failure_text = f"LLM call failed after {max_retries + 1} attempts: {_clean_error_message(error)}"
    if loop.retry_status_active and ui.via_events():
        await ui.events.emit(StatusFinished(status_id="llm:retry"))
    if ui.via_events():
        await ui.events.emit(AssistantStreamUpdated(text=failure_text))
        await ui.events.emit(AssistantStreamCommitted())
    else:
        ui.ui.error(failure_text)
    return LlmRetryResult("fail", failure_text)
