"""Tests for ReasoningEffort enum and model→effort mapping."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from voidx.config import ModelConfig, ReasoningEffort
from voidx.llm.providers.common import (
    map_effort,
    nested_reasoning,
    openai_effort,
    supported_efforts,
    thinking_toggle,
)
from voidx.llm.providers.deepseek import _reasoning as deepseek_reasoning
from voidx.llm.providers.gemini import gemini_reasoning
from voidx.llm.providers.kimi import _reasoning as kimi_reasoning
from voidx.llm.providers.anthropic import anthropic_reasoning
from voidx.llm.providers.openai import openai_reasoning


def test_reasoning_effort_values_and_default():
    assert [e.value for e in ReasoningEffort] == [
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert ModelConfig().reasoning_effort is ReasoningEffort.XHIGH


def test_model_config_rejects_aliases_and_unknown():
    for value in ("off", "ultra", "minimal", "auto", "bogus"):
        with pytest.raises(ValidationError):
            ModelConfig(reasoning_effort=value)


def test_map_effort_floor_and_ceil():
    openai_default = supported_efforts("openai", "gpt-4.1")
    assert map_effort(ReasoningEffort.MAX, openai_default) is ReasoningEffort.XHIGH
    assert map_effort(ReasoningEffort.LOW, openai_default) is ReasoningEffort.LOW

    deepseek = supported_efforts("deepseek", "deepseek-v4-pro")
    assert map_effort(ReasoningEffort.NONE, deepseek) is ReasoningEffort.NONE
    assert map_effort(ReasoningEffort.LOW, deepseek) is ReasoningEffort.HIGH
    assert map_effort(ReasoningEffort.MEDIUM, deepseek) is ReasoningEffort.HIGH
    assert map_effort(ReasoningEffort.XHIGH, deepseek) is ReasoningEffort.MAX

    kimi_k3 = supported_efforts("kimi", "kimi-k3")
    assert map_effort(ReasoningEffort.MEDIUM, kimi_k3) is ReasoningEffort.HIGH
    assert map_effort(ReasoningEffort.LOW, kimi_k3) is ReasoningEffort.LOW
    assert map_effort(ReasoningEffort.MAX, kimi_k3) is ReasoningEffort.MAX


def test_supported_efforts_model_table_and_fallback():
    assert max(supported_efforts("openai", "gpt-5.6-sol"), key=lambda e: list(ReasoningEffort).index(e)) is ReasoningEffort.MAX
    assert max(supported_efforts("openai", "gpt-5.6-terra-preview"), key=lambda e: list(ReasoningEffort).index(e)) is ReasoningEffort.MAX
    assert max(supported_efforts("openai", "gpt-5.6"), key=lambda e: list(ReasoningEffort).index(e)) is ReasoningEffort.MAX
    assert max(supported_efforts("openai", "gpt-5.5"), key=lambda e: list(ReasoningEffort).index(e)) is ReasoningEffort.XHIGH

    # Unknown provider / model → OpenAI-protocol generic cap (xhigh)
    generic = supported_efforts("custom-relay", "whatever-v1")
    assert ReasoningEffort.XHIGH in generic
    assert ReasoningEffort.MAX not in generic


def test_supported_efforts_matches_model_for_custom_providers():
    """Custom/third-party provider names still inherit model capability ladders."""
    assert max(
        supported_efforts("my-openai", "gpt-5.6-sol"),
        key=lambda e: list(ReasoningEffort).index(e),
    ) is ReasoningEffort.MAX
    assert max(
        supported_efforts("openrouter", "openai/gpt-5.6-sol"),
        key=lambda e: list(ReasoningEffort).index(e),
    ) is ReasoningEffort.MAX
    assert max(
        supported_efforts("my-claude", "claude-opus-4-8"),
        key=lambda e: list(ReasoningEffort).index(e),
    ) is ReasoningEffort.MAX
    assert max(
        supported_efforts("my-deepseek", "deepseek-v4-pro"),
        key=lambda e: list(ReasoningEffort).index(e),
    ) is ReasoningEffort.MAX
    assert max(
        supported_efforts("my-kimi", "kimi-k3"),
        key=lambda e: list(ReasoningEffort).index(e),
    ) is ReasoningEffort.MAX
    assert max(
        supported_efforts("my-gemini", "gemini-3.5-flash"),
        key=lambda e: list(ReasoningEffort).index(e),
    ) is ReasoningEffort.HIGH
    # Short ambiguous prefixes must not cross-match custom providers.
    assert ReasoningEffort.MAX not in supported_efforts("custom-relay", "foo-k3-bar")


def test_openai_reasoning_maps_by_model():
    sol = openai_reasoning(ModelConfig(provider="openai", model="gpt-5.6-sol", reasoning_effort="max"))
    assert sol == {"reasoning_effort": "max"}

    luna = openai_reasoning(ModelConfig(provider="openai", model="gpt-5.6-luna", reasoning_effort="max"))
    assert luna == {"reasoning_effort": "max"}

    base56 = openai_reasoning(ModelConfig(provider="openai", model="gpt-5.6", reasoning_effort="max"))
    assert base56 == {"reasoning_effort": "max"}

    g55 = openai_reasoning(ModelConfig(provider="openai", model="gpt-5.5", reasoning_effort="max"))
    assert g55 == {"reasoning_effort": "xhigh"}

    off = openai_reasoning(ModelConfig(provider="openai", model="gpt-5.6-sol", reasoning_effort="none"))
    assert off == {"reasoning_effort": "none"}


def test_deepseek_and_kimi_and_toggle_hooks():
    assert deepseek_reasoning(ModelConfig(provider="deepseek", model="deepseek-v4-pro", reasoning_effort="low")) == {
        "reasoning_effort": "high",
        "extra_body": {"thinking": {"type": "enabled"}},
    }
    assert deepseek_reasoning(ModelConfig(provider="deepseek", model="deepseek-v4-flash", reasoning_effort="max")) == {
        "reasoning_effort": "max",
        "extra_body": {"thinking": {"type": "enabled"}},
    }
    assert deepseek_reasoning(ModelConfig(provider="deepseek", model="deepseek-v4-pro", reasoning_effort="none")) == {
        "extra_body": {"thinking": {"type": "disabled"}},
    }

    k3 = kimi_reasoning(ModelConfig(provider="kimi", model="kimi-k3", reasoning_effort="medium"))
    assert k3 == {
        "reasoning_effort": "high",
        "extra_body": {"thinking": {"type": "enabled"}},
    }

    assert thinking_toggle(ModelConfig(provider="mimo", model="mimo-v2.5", reasoning_effort="none")) == {
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    assert thinking_toggle(ModelConfig(provider="mimo", model="mimo-v2.5", reasoning_effort="max")) == {
        "extra_body": {"thinking": {"type": "enabled"}},
    }


def test_custom_provider_hooks_clamp_effort_literals():
    """Custom provider names must not emit vendor-illegal effort literals."""
    assert deepseek_reasoning(
        ModelConfig(provider="my-deepseek", model="deepseek-v4-pro", reasoning_effort="xhigh")
    ) == {
        "reasoning_effort": "max",
        "extra_body": {"thinking": {"type": "enabled"}},
    }
    assert kimi_reasoning(
        ModelConfig(provider="my-kimi", model="kimi-k3", reasoning_effort="xhigh")
    ) == {
        "reasoning_effort": "max",
        "extra_body": {"thinking": {"type": "enabled"}},
    }
    assert anthropic_reasoning(
        ModelConfig(provider="my-claude", model="claude-opus-4-8", reasoning_effort="max")
    ) == {"thinking": {"type": "adaptive"}, "effort": "max"}
    assert openai_reasoning(
        ModelConfig(provider="my-openai", model="gpt-5.6-sol", reasoning_effort="max")
    ) == {"reasoning_effort": "max"}
    assert nested_reasoning(
        ModelConfig(provider="openrouter", model="openai/gpt-5.6-sol", reasoning_effort="max")
    ) == {"extra_body": {"reasoning": {"effort": "max"}}}


def test_gemini_and_nested_openai_fallback():
    g3 = gemini_reasoning(ModelConfig(provider="gemini", model="gemini-3-flash", reasoning_effort="max"))
    assert g3["thinking_level"] == "high"
    assert g3["include_thoughts"] is True

    g25 = gemini_reasoning(ModelConfig(provider="gemini", model="gemini-2.5-flash", reasoning_effort="high"))
    assert g25["thinking_budget"] == 16_384

    nested = nested_reasoning(ModelConfig(provider="openrouter", model="some/model", reasoning_effort="max"))
    assert nested == {"extra_body": {"reasoning": {"effort": "xhigh"}}}

    assert openai_effort("max", provider="openai", model="gpt-5.5") == "xhigh"
    assert openai_effort("max", provider="openai", model="gpt-5.6-sol") == "max"
