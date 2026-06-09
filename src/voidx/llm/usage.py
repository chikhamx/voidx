"""Token usage accounting for context and LLM calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from voidx.llm.context import count_messages_tokens


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_tokens_reported: bool = False


@dataclass
class UsageStats:
    context_tokens: int = 0
    context_limit: int = 128_000
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    last_cache_read_tokens: int = 0
    last_cache_write_tokens: int = 0
    last_estimated_cache_read_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_estimated_cache_read_tokens: int = 0
    total_cache_metric_calls: int = 0
    estimated_cache_calls: int = 0
    total_calls: int = 0
    turn_active: bool = False
    turn_start_calls: int = 0
    turn_start_input_tokens: int = 0
    turn_start_output_tokens: int = 0
    _cache_context_history: dict[str, list[list[dict[str, str]]]] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def turn_calls(self) -> int:
        return self._turn_delta("total_calls")

    @property
    def turn_input_tokens(self) -> int:
        return self._turn_delta("total_input_tokens")

    @property
    def turn_output_tokens(self) -> int:
        return self._turn_delta("total_output_tokens")

    @property
    def cache_observed_tokens(self) -> int:
        return self.total_cache_read_tokens + self.total_cache_write_tokens

    @property
    def cache_hit_rate(self) -> float | None:
        actual = self.actual_cache_hit_rate
        if actual is not None:
            return actual
        return self.estimated_cache_hit_rate

    @property
    def actual_cache_hit_rate(self) -> float | None:
        if self.total_cache_metric_calls <= 0 and self.cache_observed_tokens <= 0:
            return None
        denominator = max(self.total_input_tokens, self.cache_observed_tokens)
        if denominator <= 0:
            return None
        return self.total_cache_read_tokens / denominator

    @property
    def estimated_cache_hit_rate(self) -> float | None:
        if self.estimated_cache_calls <= 0 or self.total_input_tokens <= 0:
            return None
        return self.total_estimated_cache_read_tokens / self.total_input_tokens

    @property
    def cache_hit_rate_is_estimated(self) -> bool:
        return self.actual_cache_hit_rate is None and self.estimated_cache_hit_rate is not None

    def reset(self) -> None:
        self.context_tokens = 0
        self.last_input_tokens = 0
        self.last_output_tokens = 0
        self.last_cache_read_tokens = 0
        self.last_cache_write_tokens = 0
        self.last_estimated_cache_read_tokens = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cache_read_tokens = 0
        self.total_cache_write_tokens = 0
        self.total_estimated_cache_read_tokens = 0
        self.total_cache_metric_calls = 0
        self.estimated_cache_calls = 0
        self.total_calls = 0
        self.turn_active = False
        self.turn_start_calls = 0
        self.turn_start_input_tokens = 0
        self.turn_start_output_tokens = 0
        self._cache_context_history.clear()

    def begin_turn(self) -> None:
        self.turn_active = True
        self.turn_start_calls = self.total_calls
        self.turn_start_input_tokens = self.total_input_tokens
        self.turn_start_output_tokens = self.total_output_tokens

    def end_turn(self) -> None:
        self.turn_active = False

    def _turn_delta(self, field: str) -> int:
        if not self.turn_active:
            return 0
        start = getattr(self, f"turn_start_{field.removeprefix('total_')}", 0)
        return max(getattr(self, field, 0) - start, 0)

    def update_context(self, tokens: int, limit: int | None = None) -> None:
        self.context_tokens = max(tokens, 0)
        if limit is not None:
            self.context_limit = max(limit, 0)

    def record_call(
        self,
        usage: TokenUsage,
        *,
        fallback_input_tokens: int = 0,
        fallback_output_tokens: int = 0,
        messages: list | None = None,
        model: str = "",
        cache_key: str = "",
    ) -> None:
        cache_key = cache_key or model or "default"
        current_context = _messages_for_count(messages) if messages is not None else None
        estimated_cache_read_tokens = (
            self._estimate_cache_reuse_tokens(current_context, model, cache_key)
            if current_context is not None else None
        )
        input_tokens = usage.input_tokens or fallback_input_tokens
        output_tokens = usage.output_tokens or fallback_output_tokens
        self.last_input_tokens = max(input_tokens, 0)
        self.last_output_tokens = max(output_tokens, 0)
        self.last_cache_read_tokens = max(usage.cache_read_tokens, 0)
        self.last_cache_write_tokens = max(usage.cache_write_tokens, 0)
        self.last_estimated_cache_read_tokens = 0
        self.total_input_tokens += self.last_input_tokens
        self.total_output_tokens += self.last_output_tokens
        self.total_cache_read_tokens += self.last_cache_read_tokens
        self.total_cache_write_tokens += self.last_cache_write_tokens
        if usage.cache_tokens_reported:
            self.total_cache_metric_calls += 1
        elif estimated_cache_read_tokens is not None and self.last_input_tokens > 0:
            self.last_estimated_cache_read_tokens = min(
                max(estimated_cache_read_tokens, 0),
                self.last_input_tokens,
            )
            self.total_estimated_cache_read_tokens += self.last_estimated_cache_read_tokens
            self.estimated_cache_calls += 1
        self.total_calls += 1
        if usage.input_tokens:
            self.context_tokens = usage.input_tokens
        if current_context is not None:
            history = self._cache_context_history.setdefault(cache_key, [])
            history.append(current_context)
            del history[:-8]

    def _estimate_cache_reuse_tokens(
        self,
        current_context: list[dict[str, str]],
        model: str,
        cache_key: str,
    ) -> int:
        best = 0
        for previous_context in self._cache_context_history.get(cache_key, []):
            common_prefix = _common_prefix_context(previous_context, current_context)
            if common_prefix:
                best = max(best, count_messages_tokens(common_prefix, model))
        return best


def extract_token_usage(message: object) -> TokenUsage:
    """Extract provider token usage from LangChain message metadata."""
    usage = TokenUsage()
    sources = _usage_sources(message)
    for source in sources:
        usage.input_tokens = usage.input_tokens or _first_int(
            source,
            "input_tokens",
            "prompt_tokens",
            "input",
        )
        usage.output_tokens = usage.output_tokens or _first_int(
            source,
            "output_tokens",
            "completion_tokens",
            "output",
        )
        usage.total_tokens = usage.total_tokens or _first_int(source, "total_tokens", "total")
        usage.reasoning_tokens = usage.reasoning_tokens or _nested_first_int(
            source,
            ("output_token_details", "reasoning"),
            ("completion_tokens_details", "reasoning_tokens"),
            ("completion_token_details", "reasoning_tokens"),
            ("reasoning",),
        )
        usage.cache_read_tokens = usage.cache_read_tokens or _nested_first_int(
            source,
            ("input_token_details", "cache_read"),
            ("input_token_details", "cached_tokens"),
            ("prompt_tokens_details", "cached_tokens"),
            ("cache_read_input_tokens",),
        )
        usage.cache_write_tokens = usage.cache_write_tokens or _nested_first_int(
            source,
            ("input_token_details", "cache_creation"),
            ("cache_creation_input_tokens",),
        )
        usage.cache_tokens_reported = usage.cache_tokens_reported or _has_nested_key(
            source,
            ("input_token_details", "cache_read"),
            ("input_token_details", "cached_tokens"),
            ("prompt_tokens_details", "cached_tokens"),
            ("cache_read_input_tokens",),
            ("input_token_details", "cache_creation"),
            ("cache_creation_input_tokens",),
        )
    if not usage.total_tokens and (usage.input_tokens or usage.output_tokens):
        usage.total_tokens = usage.input_tokens + usage.output_tokens + usage.reasoning_tokens
    return usage


def estimate_context_tokens(messages: list, model: str = "") -> int:
    return count_messages_tokens(_messages_for_count(messages), model)


def estimate_message_tokens(message: object, model: str = "") -> int:
    return count_messages_tokens([_message_for_count(message)], model)


def format_token_count(value: int | None) -> str:
    value = max(int(value or 0), 0)
    if value >= 1_000_000:
        return _format_scaled(value, 1_000_000, "m")
    if value >= 1_000:
        return _format_scaled(value, 1_000, "k")
    return str(value)


def format_cache_hit_rate(stats: UsageStats) -> str:
    rate = stats.cache_hit_rate
    if rate is None:
        return "--"
    percent = max(0, min(round(rate * 100), 100))
    prefix = "~" if stats.cache_hit_rate_is_estimated else ""
    return f"{prefix}{percent}%"


def _format_scaled(value: int, divisor: int, suffix: str) -> str:
    if value % divisor == 0:
        return f"{value // divisor}{suffix}"
    return f"{value / divisor:.1f}{suffix}"


def _usage_sources(message: object) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for value in (
        getattr(message, "usage_metadata", None),
        getattr(message, "response_metadata", None),
        getattr(message, "additional_kwargs", None),
    ):
        if isinstance(value, dict):
            sources.append(value)
            for key in ("usage", "token_usage"):
                nested = value.get(key)
                if isinstance(nested, dict):
                    sources.append(nested)
    return sources


def _first_int(source: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = source.get(key)
        if isinstance(value, int):
            return value
    return 0


def _nested_first_int(source: dict[str, Any], *paths: tuple[str, ...]) -> int:
    for path in paths:
        current: Any = source
        for part in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(part)
        if isinstance(current, int):
            return current
    return 0


def _has_nested_key(source: dict[str, Any], *paths: tuple[str, ...]) -> bool:
    for path in paths:
        current: Any = source
        for part in path:
            if not isinstance(current, dict) or part not in current:
                current = None
                break
            current = current[part]
        if current is not None:
            return True
    return False


def _messages_for_count(messages: list) -> list[dict[str, str]]:
    return [_message_for_count(message) for message in messages]


def _common_prefix_context(
    previous_context: list[dict[str, str]],
    current_context: list[dict[str, str]],
) -> list[dict[str, str]]:
    common: list[dict[str, str]] = []
    for previous, current in zip(previous_context, current_context):
        if previous != current:
            break
        common.append(current)
    return common


def _message_for_count(message: object) -> dict[str, str]:
    role = getattr(message, "type", "") or message.__class__.__name__.removesuffix("Message").lower()
    parts = [str(_message_content_for_count(message))]
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        parts.append(str(tool_calls))
    return {"role": role, "content": "\n".join(parts)}


def _message_content_for_count(message: object) -> object:
    if isinstance(message, dict):
        return message.get("content", "")
    return _content_for_count(getattr(message, "content", str(message)))


def _content_for_count(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif item.get("type") in {"image", "image_url"}:
                    parts.append("[image]")
                else:
                    parts.append(str(item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)
