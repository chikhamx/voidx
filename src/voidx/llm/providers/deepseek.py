"""DeepSeek — plus the shared ChatOpenAI subclass for the deepseek protocol.

:class:`DeepSeekChatOpenAI` serves all China-domestic OpenAI-compatible
providers (deepseek, qwen, zhipu, kimi, doubao, mimo, longcat, typex,
minimax).  It solves two problems common to the whole family:

1. **Streaming reasoning_content loss** — LangChain's
   ``_convert_delta_to_message_chunk`` silently drops ``reasoning_content``
   from the streaming delta.  We intercept the raw chunk and inject it into
   ``additional_kwargs`` so that ``_extract_thinking_openai`` can find it.

2. **Provider-specific reasoning parameters** — each provider has its own
   ``extra_body`` schema for enabling/disabling thinking and mapping effort
   levels.  :meth:`reasoning_kwargs` dispatches to the registered provider
   hook, falling back to the DeepSeek format for unknown providers.
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from voidx.config import ModelConfig
from voidx.llm.providers import base
from voidx.llm.providers.base import PROTOCOL_DEEPSEEK, ProviderSpec
from voidx.llm.providers.common import normalized_effort, preserve_reasoning_delta


class DeepSeekChatOpenAI(ChatOpenAI):
    """Unified ``ChatOpenAI`` subclass for China-domestic providers."""

    @property
    def has_active_reasoning(self) -> bool:
        """Return whether the current request has reasoning enabled."""
        if getattr(self, "reasoning_effort", None):
            return True
        extra = getattr(self, "extra_body", None) or {}
        if extra.get("enable_thinking"):
            return True
        thinking = extra.get("thinking", {})
        return isinstance(thinking, dict) and thinking.get("type") in ("enabled", "auto")

    @property
    def resolver_structured_output_method(self) -> str:
        return "json_mode" if self.has_active_reasoning else "function_calling"

    # ── streaming reasoning_content preservation ──────────────────────────

    def _convert_chunk_to_generation_chunk(  # type: ignore[override]
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation_chunk is None:
            return None

        msg = generation_chunk.message
        if not isinstance(msg, AIMessageChunk):
            return generation_chunk

        # Extract reasoning_content from the raw delta dict
        choices = chunk.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            preserve_reasoning_delta(msg, delta)

        return generation_chunk

    # ── multi-turn reasoning_content injection ──────────────────────────────

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        """Inject reasoning_content into assistant message dicts.

        DeepSeek's thinking mode requires ``reasoning_content`` to be passed
        back as a top-level field on assistant messages in multi-turn
        conversations.  LangChain's ``_convert_message_to_dict`` silently
        drops ``additional_kwargs.reasoning_content``, so we re-inject it
        after the parent builds the payload.
        """
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return payload

        original_messages = self._convert_input(input_).to_messages()
        for i, msg_dict in enumerate(messages):
            if not isinstance(msg_dict, dict) or msg_dict.get("role") != "assistant":
                continue
            if i >= len(original_messages):
                break
            orig = original_messages[i]
            rc = getattr(orig, "additional_kwargs", {}).get("reasoning_content")
            if isinstance(rc, str) and rc:
                msg_dict["reasoning_content"] = rc

        return payload

    # ── provider-specific reasoning effort mapping ────────────────────────

    @staticmethod
    def reasoning_kwargs(config: ModelConfig) -> dict:
        """Return reasoning kwargs for China-domestic providers.

        Dispatches to the registered provider hook (see each provider
        module).  Unknown providers with the deepseek protocol fall back
        to the DeepSeek format.
        """
        spec = base.get(config.provider)
        if spec is not None and spec.reasoning is not None:
            return spec.reasoning(config)
        return _reasoning(config)


def _reasoning(config: ModelConfig) -> dict:
    """DeepSeek format: ``reasoning_effort`` top-level + ``extra_body.thinking.type``."""
    effort = normalized_effort(config.reasoning_effort)
    if effort is None:
        return {}
    if effort == "none":
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    ds_effort = "max" if effort in ("xhigh", "max") else "high"
    return {"reasoning_effort": ds_effort, "extra_body": {"thinking": {"type": "enabled"}}}


def _temperature_override(config: ModelConfig) -> float | None:
    """DeepSeek reasoner models require temperature to be unset."""
    if "reasoner" in config.model.lower():
        return None
    return config.temperature


base.register(ProviderSpec(
    name="deepseek",
    protocol=PROTOCOL_DEEPSEEK,
    default_base_url="https://api.deepseek.com/v1",
    context_limit=1_000_000,
    static_models=(
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    ),
    reasoning=_reasoning,
    temperature_override=_temperature_override,
))
