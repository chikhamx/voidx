"""Shared helpers for provider reasoning/streaming implementations.

Provider modules import from here (never from each other or from
``voidx.llm.adapters.langchain_model_factory``) to keep the dependency direction one-way.
"""

from __future__ import annotations

from langchain_core.messages import AIMessageChunk

from voidx.llm.domain.model import ModelConfig, ReasoningEffort

REASONING_EFFORT_ORDER: tuple[ReasoningEffort, ...] = tuple(ReasoningEffort)

# Budget tokens shared by Anthropic-style thinking and Qwen thinking_budget.
ANTHROPIC_BUDGETS = {
    ReasoningEffort.LOW: 1_024,
    ReasoningEffort.MEDIUM: 4_096,
    ReasoningEffort.HIGH: 8_192,
    ReasoningEffort.XHIGH: 8_192,
    ReasoningEffort.MAX: 8_192,
}

GEMINI_THINKING_BUDGETS = {
    ReasoningEffort.LOW: 4_096,
    ReasoningEffort.MEDIUM: 8_192,
    ReasoningEffort.HIGH: 16_384,
    ReasoningEffort.XHIGH: 32_768,
    ReasoningEffort.MAX: 65_536,
}

# OpenAI-protocol generic cap used as the final fallback.
_OPENAI_GENERIC: tuple[ReasoningEffort, ...] = (
    ReasoningEffort.NONE,
    ReasoningEffort.LOW,
    ReasoningEffort.MEDIUM,
    ReasoningEffort.HIGH,
    ReasoningEffort.XHIGH,
)

_TOGGLE_ONLY: tuple[ReasoningEffort, ...] = (
    ReasoningEffort.NONE,
    ReasoningEffort.HIGH,
)


_GEMINI_BUDGET: tuple[ReasoningEffort, ...] = (
    ReasoningEffort.NONE,
    ReasoningEffort.LOW,
    ReasoningEffort.MEDIUM,
    ReasoningEffort.HIGH,
    ReasoningEffort.XHIGH,
    ReasoningEffort.MAX,
)

_LEVEL_TO_HIGH: tuple[ReasoningEffort, ...] = (
    ReasoningEffort.NONE,
    ReasoningEffort.LOW,
    ReasoningEffort.MEDIUM,
    ReasoningEffort.HIGH,
)

_DEEPSEEK: tuple[ReasoningEffort, ...] = (
    ReasoningEffort.NONE,
    ReasoningEffort.HIGH,
    ReasoningEffort.MAX,
)

_KIMI_K3: tuple[ReasoningEffort, ...] = (
    ReasoningEffort.NONE,
    ReasoningEffort.LOW,
    ReasoningEffort.HIGH,
    ReasoningEffort.MAX,
)

_CLAUDE_ADAPTIVE: tuple[ReasoningEffort, ...] = (
    ReasoningEffort.NONE,
    ReasoningEffort.LOW,
    ReasoningEffort.MEDIUM,
    ReasoningEffort.HIGH,
    ReasoningEffort.XHIGH,
    ReasoningEffort.MAX,
)

_OPENAI_TO_MAX: tuple[ReasoningEffort, ...] = (
    ReasoningEffort.NONE,
    ReasoningEffort.LOW,
    ReasoningEffort.MEDIUM,
    ReasoningEffort.HIGH,
    ReasoningEffort.XHIGH,
    ReasoningEffort.MAX,
)

# Specific model prefixes first (longest / most specific wins via scan order).
# provider=None means match any provider (custom relays / openrouter / aliases).
# Keep prefixes specific enough to avoid cross-family false positives.
# (provider_or_None, model_substring, supported)
_MODEL_EFFORT_TABLE: tuple[tuple[str | None, str, tuple[ReasoningEffort, ...]], ...] = (
    (None, "gpt-5.6-sol", _OPENAI_TO_MAX),
    (None, "gpt-5.6-terra", _OPENAI_TO_MAX),
    (None, "gpt-5.6", _OPENAI_TO_MAX),
    (None, "gpt-5.5", _OPENAI_GENERIC),
    (None, "claude-opus-5", _CLAUDE_ADAPTIVE),
    (None, "claude-sonnet-5", _CLAUDE_ADAPTIVE),
    (None, "claude-opus-4", _CLAUDE_ADAPTIVE),
    (None, "gemini-3", _LEVEL_TO_HIGH),
    (None, "gemini-4", _LEVEL_TO_HIGH),
    (None, "gemini-2.5", _GEMINI_BUDGET),
    (None, "deepseek-v4", _DEEPSEEK),
    (None, "kimi-k3", _KIMI_K3),
    # Short "k3" is too ambiguous for any-provider matching; keep kimi-scoped.
    ("kimi", "k3", _KIMI_K3),
    (None, "qwen3", _TOGGLE_ONLY),
    (None, "qwq", _TOGGLE_ONLY),
    (None, "doubao-seed", _TOGGLE_ONLY),
    (None, "seed-1.6", _TOGGLE_ONLY),
    (None, "glm-5", _TOGGLE_ONLY),
    (None, "glm-4.7", _TOGGLE_ONLY),
    (None, "glm-4.6", _TOGGLE_ONLY),
    (None, "glm-4.5", _TOGGLE_ONLY),
    (None, "glm-4", _TOGGLE_ONLY),
    (None, "minimax-m3", _TOGGLE_ONLY),
    (None, "mimo-v2.5", _TOGGLE_ONLY),
)

_PROVIDER_EFFORT_DEFAULTS: dict[str, tuple[ReasoningEffort, ...]] = {
    "openai": _OPENAI_GENERIC,
    "openrouter": _OPENAI_GENERIC,
    "xunfei-coding-plan": _OPENAI_GENERIC,
    "anthropic": _CLAUDE_ADAPTIVE,
    "gemini": _GEMINI_BUDGET,
    "deepseek": _DEEPSEEK,
    "kimi": _TOGGLE_ONLY,
    "qwen": _TOGGLE_ONLY,
    "doubao": _TOGGLE_ONLY,
    "zhipu": _TOGGLE_ONLY,
    "typex": _TOGGLE_ONLY,
    "mimo": _TOGGLE_ONLY,
    "mimo-token-plan": _TOGGLE_ONLY,
    "longcat": _TOGGLE_ONLY,
    "minimax": _TOGGLE_ONLY,
}


def parse_reasoning_effort(value: ReasoningEffort | str | None) -> ReasoningEffort:
    """Parse a strict ReasoningEffort value. No aliases."""
    if isinstance(value, ReasoningEffort):
        return value
    if value is None:
        return ReasoningEffort.XHIGH
    return ReasoningEffort(str(value).strip().lower())


def _effort_rank(effort: ReasoningEffort) -> int:
    return REASONING_EFFORT_ORDER.index(effort)


def map_effort(
    requested: ReasoningEffort,
    supported: tuple[ReasoningEffort, ...] | list[ReasoningEffort],
) -> ReasoningEffort:
    """Map *requested* onto *supported*.

    Exact match wins. ``none`` only maps to ``none`` (or the lowest supported
    value if ``none`` is absent). All other values pick the closest supported
    non-``none`` level; equidistant ties prefer the higher level so sparse
    provider ladders (e.g. DeepSeek high/max, Kimi low/high/max) follow the
    vendor's upward banding.
    """
    if not supported:
        return ReasoningEffort.NONE
    ordered = sorted(set(supported), key=_effort_rank)
    if requested in ordered:
        return requested
    if requested is ReasoningEffort.NONE:
        return ReasoningEffort.NONE if ReasoningEffort.NONE in ordered else ordered[0]

    candidates = [e for e in ordered if e is not ReasoningEffort.NONE]
    if not candidates:
        return ordered[0]

    req_rank = _effort_rank(requested)
    best = candidates[0]
    best_dist = abs(_effort_rank(best) - req_rank)
    for effort in candidates[1:]:
        dist = abs(_effort_rank(effort) - req_rank)
        if dist < best_dist or (dist == best_dist and _effort_rank(effort) > _effort_rank(best)):
            best = effort
            best_dist = dist
    return best


def _normalize_model_name(model: str) -> str:
    """Normalize model ids like ``openai/gpt-5.6-sol`` or ``models/gemini-3``."""
    name = (model or "").lower().strip()
    if name.startswith("models/"):
        name = name[len("models/"):]
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name


def supported_efforts(provider: str, model: str) -> tuple[ReasoningEffort, ...]:
    """Resolve supported efforts: model table → provider default → OpenAI generic.

    Model-table entries with ``provider=None`` match any provider so custom
    relays and OpenRouter inherit the same capability ladder as first-party
    names. Provider-scoped entries still win when present.
    """
    name = _normalize_model_name(model)
    raw_name = (model or "").lower()
    prov = (provider or "").lower()
    for table_provider, prefix, supported in _MODEL_EFFORT_TABLE:
        if table_provider is not None and table_provider != prov:
            continue
        # Prefer normalized bare model id; also allow raw path matches.
        if prefix in name or prefix in raw_name:
            return supported
    if prov in _PROVIDER_EFFORT_DEFAULTS:
        return _PROVIDER_EFFORT_DEFAULTS[prov]
    return _OPENAI_GENERIC


def resolve_effort(config: ModelConfig) -> ReasoningEffort:
    """Parse config effort and clamp to the current provider/model capability."""
    requested = parse_reasoning_effort(config.reasoning_effort)
    return map_effort(requested, supported_efforts(config.provider, config.model))


def normalized_effort(effort: ReasoningEffort | str | None) -> str:
    """Return the canonical effort string (strict; no aliases)."""
    return parse_reasoning_effort(effort).value


def openai_effort(
    effort: ReasoningEffort | str | None,
    *,
    provider: str = "openai",
    model: str = "",
) -> str:
    """Map unified effort to an OpenAI nested ``reasoning.effort`` string."""
    requested = parse_reasoning_effort(effort)
    mapped = map_effort(requested, supported_efforts(provider, model))
    return mapped.value


def thinking_toggle(config: ModelConfig) -> dict:
    """``extra_body.thinking`` enabled/disabled for compatible providers."""
    effort = resolve_effort(config)
    if effort is ReasoningEffort.NONE:
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {"extra_body": {"thinking": {"type": "enabled"}}}


def nested_reasoning(config: ModelConfig) -> dict:
    """Map reasoning effort to ``extra_body.reasoning.effort``."""
    effort = openai_effort(config.reasoning_effort, provider=config.provider, model=config.model)
    return {"extra_body": {"reasoning": {"effort": effort}}}


def zhipu_reasoning(config: ModelConfig) -> dict:
    """Enable GLM thinking only for models that support it."""
    if not supports_zhipu_thinking(config.model):
        return {}
    return thinking_toggle(config)


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
