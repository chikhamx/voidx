"""Tests for Gemini provider integration.

Covers: protocol resolution, reasoning kwargs (2.5 and 3+ paths),
thinking extraction, context limit, model factory, and ImportError fallback.
"""

from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessageChunk

from voidx.config import ModelConfig
from voidx.llm.provider import (
    extract_thinking,
    get_context_limit,
    resolve_protocol,
    _gemini_reasoning_kwargs,
    _is_gemini3_plus,
)


# ── protocol resolution ──────────────────────────────────────────────────


def test_resolve_protocol_gemini():
    assert resolve_protocol(ModelConfig(provider="gemini")) == "gemini"


def test_resolve_protocol_gemini_explicit_protocol():
    config = ModelConfig(provider="gemini", protocol="gemini")
    assert resolve_protocol(config) == "gemini"


# ── _is_gemini3_plus ────────────────────────────────────────────────────


def test_is_gemini3_plus_identifies_3x_models():
    assert _is_gemini3_plus("gemini-3-pro") is True
    assert _is_gemini3_plus("gemini-3-flash") is True
    assert _is_gemini3_plus("gemini-4-pro") is True


def test_is_gemini3_plus_rejects_2x_models():
    assert _is_gemini3_plus("gemini-2.5-pro") is False
    assert _is_gemini3_plus("gemini-2.5-flash") is False
    assert _is_gemini3_plus("gemini-2.0-flash") is False


# ── reasoning kwargs: Gemini 2.5 (thinking_budget) ──────────────────────


def test_gemini_reasoning_kwargs_25_medium():
    config = ModelConfig(provider="gemini", model="gemini-2.5-flash", reasoning_effort="medium")
    kwargs = _gemini_reasoning_kwargs(config)
    assert kwargs == {"include_thoughts": True, "thinking_budget": 8_192}


def test_gemini_reasoning_kwargs_25_high():
    config = ModelConfig(provider="gemini", model="gemini-2.5-pro", reasoning_effort="high")
    kwargs = _gemini_reasoning_kwargs(config)
    assert kwargs == {"include_thoughts": True, "thinking_budget": 16_384}


def test_gemini_reasoning_kwargs_25_low():
    config = ModelConfig(provider="gemini", model="gemini-2.5-flash", reasoning_effort="low")
    kwargs = _gemini_reasoning_kwargs(config)
    assert kwargs == {"include_thoughts": True, "thinking_budget": 4_096}


def test_gemini_reasoning_kwargs_25_minimal():
    config = ModelConfig(provider="gemini", model="gemini-2.5-flash", reasoning_effort="minimal")
    kwargs = _gemini_reasoning_kwargs(config)
    assert kwargs == {"include_thoughts": True, "thinking_budget": 1_024}


def test_gemini_reasoning_kwargs_25_xhigh():
    config = ModelConfig(provider="gemini", model="gemini-2.5-pro", reasoning_effort="xhigh")
    kwargs = _gemini_reasoning_kwargs(config)
    assert kwargs == {"include_thoughts": True, "thinking_budget": 32_768}


def test_gemini_reasoning_kwargs_25_max():
    config = ModelConfig(provider="gemini", model="gemini-2.5-pro", reasoning_effort="max")
    kwargs = _gemini_reasoning_kwargs(config)
    assert kwargs == {"include_thoughts": True, "thinking_budget": 65_536}


# ── reasoning kwargs: Gemini 3+ (thinking_level) ────────────────────────


def test_gemini_reasoning_kwargs_3_medium():
    config = ModelConfig(provider="gemini", model="gemini-3-pro", reasoning_effort="medium")
    kwargs = _gemini_reasoning_kwargs(config)
    assert kwargs == {"include_thoughts": True, "thinking_level": "medium"}


def test_gemini_reasoning_kwargs_3_high():
    config = ModelConfig(provider="gemini", model="gemini-3-flash", reasoning_effort="high")
    kwargs = _gemini_reasoning_kwargs(config)
    assert kwargs == {"include_thoughts": True, "thinking_level": "high"}


def test_gemini_reasoning_kwargs_3_low():
    config = ModelConfig(provider="gemini", model="gemini-3-pro", reasoning_effort="low")
    kwargs = _gemini_reasoning_kwargs(config)
    assert kwargs == {"include_thoughts": True, "thinking_level": "low"}


def test_gemini_reasoning_kwargs_3_minimal():
    config = ModelConfig(provider="gemini", model="gemini-3-pro", reasoning_effort="minimal")
    kwargs = _gemini_reasoning_kwargs(config)
    assert kwargs == {"include_thoughts": True, "thinking_level": "minimal"}


def test_gemini_reasoning_kwargs_3_xhigh_maps_to_high():
    config = ModelConfig(provider="gemini", model="gemini-3-pro", reasoning_effort="xhigh")
    kwargs = _gemini_reasoning_kwargs(config)
    assert kwargs == {"include_thoughts": True, "thinking_level": "high"}


def test_gemini_reasoning_kwargs_3_max_maps_to_high():
    config = ModelConfig(provider="gemini", model="gemini-3-pro", reasoning_effort="max")
    kwargs = _gemini_reasoning_kwargs(config)
    assert kwargs == {"include_thoughts": True, "thinking_level": "high"}


# ── reasoning kwargs: effort off/none ────────────────────────────────────


def test_gemini_reasoning_kwargs_default_effort():
    """Default reasoning_effort is 'xhigh', so kwargs should reflect that."""
    config = ModelConfig(provider="gemini", model="gemini-2.5-flash")
    kwargs = _gemini_reasoning_kwargs(config)
    assert kwargs == {"include_thoughts": True, "thinking_budget": 32_768}


def test_gemini_reasoning_kwargs_off_effort():
    config = ModelConfig(provider="gemini", model="gemini-2.5-flash", reasoning_effort="off")
    kwargs = _gemini_reasoning_kwargs(config)
    assert kwargs == {}


def test_gemini_reasoning_kwargs_none_string_effort():
    config = ModelConfig(provider="gemini", model="gemini-2.5-flash", reasoning_effort="none")
    kwargs = _gemini_reasoning_kwargs(config)
    assert kwargs == {}


# ── thinking extraction ──────────────────────────────────────────────────


def test_extract_thinking_gemini_v0_format():
    """Gemini v0 format: type='thinking', 'thinking' key holds the text."""
    chunk = AIMessageChunk(content=[
        {"type": "thinking", "thinking": "gemini thought text", "signature": "abc"},
    ])
    assert extract_thinking(chunk, "gemini") == "gemini thought text"


def test_extract_thinking_gemini_v1_format():
    """Gemini v1 format: type='reasoning', 'reasoning' key holds the text."""
    chunk = AIMessageChunk(content=[
        {"type": "reasoning", "reasoning": "gemini reasoning text", "extras": {"signature": "abc"}},
    ])
    assert extract_thinking(chunk, "gemini") == "gemini reasoning text"


def test_extract_thinking_gemini_no_thinking():
    chunk = AIMessageChunk(content="just a response")
    assert extract_thinking(chunk, "gemini") == ""


# ── context limit ────────────────────────────────────────────────────────


def test_get_context_limit_gemini():
    assert get_context_limit("gemini") == 1_000_000


# ── model factory ────────────────────────────────────────────────────────


def test_create_chat_model_gemini():
    """create_chat_model with gemini protocol returns ChatGoogleGenerativeAI."""
    from voidx.llm.provider import create_chat_model

    mock_cls = MagicMock()
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    with patch.dict("sys.modules", {"langchain_google_genai": MagicMock(ChatGoogleGenerativeAI=mock_cls)}):
        with patch("voidx.llm.provider.resolve_protocol", return_value="gemini"):
            model = create_chat_model(
                "test-api-key",
                ModelConfig(provider="gemini", model="gemini-2.5-flash"),
            )
            assert model is mock_instance
            mock_cls.assert_called_once()
            call_kwargs = mock_cls.call_args[1]
            assert call_kwargs["model"] == "gemini-2.5-flash"
            assert call_kwargs["api_key"] == "test-api-key"


def test_create_chat_model_gemini_with_reasoning():
    """Gemini model with reasoning_effort should include thinking kwargs."""
    from voidx.llm.provider import create_chat_model

    mock_cls = MagicMock()
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    with patch.dict("sys.modules", {"langchain_google_genai": MagicMock(ChatGoogleGenerativeAI=mock_cls)}):
        with patch("voidx.llm.provider.resolve_protocol", return_value="gemini"):
            model = create_chat_model(
                "test-api-key",
                ModelConfig(provider="gemini", model="gemini-2.5-flash", reasoning_effort="high"),
            )
            call_kwargs = mock_cls.call_args[1]
            assert call_kwargs["thinking_budget"] == 16_384
            assert call_kwargs["include_thoughts"] is True


def test_create_chat_model_gemini_import_error():
    """Missing langchain-google-genai should raise ImportError with install hint."""
    from voidx.llm.provider import create_chat_model

    with patch("voidx.llm.provider.resolve_protocol", return_value="gemini"):
        with patch.dict("sys.modules", {"langchain_google_genai": None}):
            try:
                create_chat_model(
                    "test-key",
                    ModelConfig(provider="gemini", model="gemini-2.5-flash"),
                )
                assert False, "Should have raised ImportError"
            except ImportError as e:
                assert "langchain-google-genai" in str(e)
                assert "voidx[gemini]" in str(e)
