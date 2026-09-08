"""Immutable prepared main request contract for LLM calls and budget decisions."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping

from langchain_core.messages import BaseMessage

from voidx.llm.compaction.constants import COMPACTION_BUFFER
from voidx.llm.usage import estimate_context_tokens_with_tools


@dataclass(frozen=True)
class PreparedMainRequest:
    """Provider request snapshot with a separate logical budget view."""

    model_name: str
    messages: list[BaseMessage]
    budget_messages: list[BaseMessage]
    tool_defs: list[dict[str, Any]]
    context_limit: int
    main_output_reserve: int
    safety_margin: int
    total_input_tokens: int
    main_request_limit: int
    should_rollover: bool
    request_hash: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


def prepare_main_request(
    messages: list[BaseMessage],
    tool_defs: list[dict[str, Any]],
    *,
    model_name: str,
    context_limit: int,
    output_token_max: int = 4096,
    safety_margin: int = COMPACTION_BUFFER,
    token_counter: Callable[[list[BaseMessage], str], int] | None = None,
    budget_messages: list[BaseMessage] | None = None,
    metadata: dict[str, Any] | None = None,
) -> PreparedMainRequest:
    """Construct a stable provider snapshot and calculate its logical budget."""
    provider_messages = deepcopy(list(messages))
    logical_messages = deepcopy(provider_messages)
    provider_tools = deepcopy(list(tool_defs))
    meta = deepcopy(dict(metadata or {}))
    if token_counter is not None:
        meta["token_counter"] = token_counter

    if token_counter is not None:
        total_tokens = token_counter(logical_messages, model_name)
        total_tokens += estimate_context_tokens_with_tools([], provider_tools, model_name)
    else:
        total_tokens = estimate_context_tokens_with_tools(
            logical_messages,
            provider_tools,
            model_name,
        )

    request_limit = max(0, context_limit - output_token_max - safety_margin)
    should_rollover = total_tokens >= request_limit if context_limit > 0 else False

    msg_hashes: list[dict[str, Any]] = []
    for msg in provider_messages:
        msg_hashes.append({
            "role": msg.__class__.__name__,
            "content": msg.content,
            "additional_kwargs": getattr(msg, "additional_kwargs", {}) or {},
            "response_metadata": getattr(msg, "response_metadata", {}) or {},
            "name": getattr(msg, "name", None),
            "tool_calls": getattr(msg, "tool_calls", None) or [],
            "invalid_tool_calls": getattr(msg, "invalid_tool_calls", None) or [],
            "tool_call_id": getattr(msg, "tool_call_id", None) or "",
        })
    hash_payload = json.dumps(
        {
            "model": model_name,
            "tools": provider_tools,
            "messages": msg_hashes,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    request_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()

    return PreparedMainRequest(
        model_name=model_name,
        messages=provider_messages,
        budget_messages=logical_messages,
        tool_defs=provider_tools,
        context_limit=context_limit,
        main_output_reserve=output_token_max,
        safety_margin=safety_margin,
        total_input_tokens=total_tokens,
        main_request_limit=request_limit,
        should_rollover=should_rollover,
        request_hash=request_hash,
        metadata=MappingProxyType(meta),
    )
