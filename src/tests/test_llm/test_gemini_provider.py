"""Tests for Gemini provider integration.

Covers: protocol resolution, reasoning kwargs (2.5 and 3+ paths),
thinking extraction, context limit, model factory, and ImportError fallback.
"""

from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessageChunk

from voidx.config import ModelConfig
from voidx.llm.provider import (
    create_chat_model,
    create_resolver_model,
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


def test_create_resolver_model_disables_gemini_thinking():
    config = ModelConfig(
        provider="gemini",
        model="gemini-2.5-pro",
        reasoning_effort="high",
    )
    model = create_chat_model("test-key", config)

    resolver_model = create_resolver_model(model, config)

    assert model.thinking_budget == 16_384
    assert model.include_thoughts is True
    assert resolver_model.thinking_budget is None
    assert resolver_model.thinking_level is None
    assert resolver_model.include_thoughts is None


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


def test_get_context_limit_with_override():
    """context_window 参数有效时直接返回，覆盖 provider 查表。"""
    assert get_context_limit("gemini", context_window=128_000) == 128_000
    assert get_context_limit("openai", context_window=512_000) == 512_000


def test_get_context_limit_override_none_falls_back():
    """context_window 为 None 时走原 provider 查表（向后兼容）。"""
    assert get_context_limit("gemini", context_window=None) == 1_000_000
    assert get_context_limit("anthropic", context_window=None) == 200_000


def test_get_context_limit_override_zero_falls_back():
    """context_window 为 0 时视为无效，走回退。"""
    assert get_context_limit("gemini", context_window=0) == 1_000_000
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


def test_create_chat_model_gemini_strips_v1beta_suffix_from_base_url():
    """base_url ending with /v1beta must be stripped — google-genai SDK appends it."""
    from voidx.llm.provider import create_chat_model

    mock_cls = MagicMock()
    mock_cls.return_value = MagicMock()

    with patch.dict("sys.modules", {"langchain_google_genai": MagicMock(ChatGoogleGenerativeAI=mock_cls)}):
        with patch("voidx.llm.provider.resolve_protocol", return_value="gemini"):
            create_chat_model(
                "test-api-key",
                ModelConfig(
                    provider="gemini",
                    model="gemini-2.5-flash",
                    base_url="http://relay.example.com/antigravity/v1beta",
                ),
            )
            call_kwargs = mock_cls.call_args[1]
            assert call_kwargs["base_url"] == "http://relay.example.com/antigravity"


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
    """When _ensure_gemini_dep fails (auto-install exhausted), create_chat_model
    should raise ImportError with install hint."""
    from voidx.llm.provider import create_chat_model

    with patch("voidx.llm.provider.resolve_protocol", return_value="gemini"):
        with patch("voidx.llm.provider._ensure_gemini_dep", side_effect=ImportError(
            "langchain-google-genai is required for Gemini protocol. "
            "Install with: pip install voidx[gemini]"
        )):
            try:
                create_chat_model(
                    "test-key",
                    ModelConfig(provider="gemini", model="gemini-2.5-flash"),
                )
                assert False, "Should have raised ImportError"
            except ImportError as e:
                assert "langchain-google-genai" in str(e)
                assert "voidx[gemini]" in str(e)


# ── _ensure_gemini_dep auto-install ─────────────────────────────────────


def test_ensure_gemini_dep_already_installed():
    """When langchain_google_genai is already importable, no subprocess call."""
    from voidx.llm.provider import _ensure_gemini_dep

    mock_cls = MagicMock()
    with patch.dict("sys.modules", {"langchain_google_genai": MagicMock(ChatGoogleGenerativeAI=mock_cls)}):
        with patch("subprocess.run") as mock_run:
            _ensure_gemini_dep()
            mock_run.assert_not_called()


def test_ensure_gemini_dep_auto_install_success():
    """When import fails initially, pip install runs and import succeeds on retry."""
    from voidx.llm.provider import _ensure_gemini_dep

    mock_cls = MagicMock()
    original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    call_count = {"import": 0}

    def fake_import(name, *args, **kwargs):
        if name == "langchain_google_genai":
            call_count["import"] += 1
            if call_count["import"] == 1:
                raise ImportError("not installed")
            return MagicMock(ChatGoogleGenerativeAI=mock_cls)
        return original_import(name, *args, **kwargs)

    mock_result = MagicMock()
    mock_result.returncode = 0

    with patch("builtins.__import__", side_effect=fake_import):
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            _ensure_gemini_dep()
            assert mock_run.call_count == 1


def test_ensure_gemini_dep_retry_then_fail():
    """When pip install fails 3 times, ImportError is raised with install hint."""
    from voidx.llm.provider import _ensure_gemini_dep

    original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    def fake_import(name, *args, **kwargs):
        if name == "langchain_google_genai":
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "network error"

    with patch("builtins.__import__", side_effect=fake_import):
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            try:
                _ensure_gemini_dep()
                assert False, "Should have raised ImportError"
            except ImportError as e:
                assert "langchain-google-genai" in str(e)
                assert "voidx[gemini]" in str(e)
                assert mock_run.call_count == 3


def test_ensure_gemini_dep_timeout_retries_then_fail():
    """When pip install times out 3 times, ImportError is raised with timeout hint."""
    from voidx.llm.provider import _ensure_gemini_dep
    import subprocess

    original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    def fake_import(name, *args, **kwargs):
        if name == "langchain_google_genai":
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pip", timeout=120)) as mock_run:
            try:
                _ensure_gemini_dep()
                assert False, "Should have raised ImportError"
            except ImportError as e:
                assert "timed out" in str(e)
                assert "voidx[gemini]" in str(e)
                assert mock_run.call_count == 3
