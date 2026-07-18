"""Shared helpers for provider reasoning/streaming implementations.

Provider modules import from here (never from each other or from
``voidx.llm.provider``) to keep the dependency direction one-way.
"""

from __future__ import annotations

from langchain_core.messages import AIMessageChunk

from voidx.config import ModelConfig

# Budget tokens shared by Anthropic-style thinking and Qwen thinking_budget.
ANTHROPIC_BUDGETS = {
    "low": 1_024,
    "medium": 4_096,
    "high": 8_192,
}


def normalized_effort(effort: str | None) -> str | None:
    if effort is None:
        return None
    value = effort.strip().lower()
    if value in {"", "off", "none"}:
        return "none"
    if value in {"minimal", "low", "medium", "high", "xhigh", "max"}:
        return value
    return "medium"


def openai_effort(effort: str | None) -> str | None:
    """Map unified reasoning_effort to an effort string for the nested reasoning format."""
    value = normalized_effort(effort)
    if value is None:
        return None
    return {"none": "none", "minimal": "minimal", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "high"}.get(value)


def thinking_toggle(config: ModelConfig) -> dict:
    """``extra_body.thinking`` enabled/disabled for deepseek-protocol providers."""
    effort = normalized_effort(config.reasoning_effort)
    if effort is None:
        return {}
    if effort == "none":
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {"extra_body": {"thinking": {"type": "enabled"}}}


_ZHIPU_THINKING_MODELS = (
    "glm-4.5",
    "glm-4.6",
    "glm-4.7",
    "glm-5",
)


def supports_zhipu_thinking(model: str) -> bool:
    """GLM-model gate shared by zhipu and typex (typex serves GLM models)."""
    name = model.lower()
    return any(p in name for p in _ZHIPU_THINKING_MODELS)


def preserve_reasoning_delta(msg: AIMessageChunk, delta: dict) -> None:
    """Inject reasoning fields from a raw streaming delta into additional_kwargs."""
    rc = delta.get("reasoning_content")
    if isinstance(rc, str) and rc:
        msg.additional_kwargs["reasoning_content"] = rc

    reasoning = delta.get("reasoning")
    if reasoning:
        msg.additional_kwargs["reasoning"] = reasoning

    thinking = delta.get("thinking")
    if thinking:
        msg.additional_kwargs["thinking"] = thinking

    rd = delta.get("reasoning_details")
    if isinstance(rd, list) and rd:
        items = [
            item for item in rd
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"]
        ]
        if items:
            msg.additional_kwargs["reasoning_details"] = items
