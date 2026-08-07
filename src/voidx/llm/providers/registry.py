"""Explicit loading of built-in provider registrations."""

from __future__ import annotations


def load_builtins() -> None:
    """Import every built-in provider so its explicit registration is applied."""
    import voidx.llm.providers.anthropic as _anthropic
    import voidx.llm.providers.deepseek as _deepseek
    import voidx.llm.providers.doubao as _doubao
    import voidx.llm.providers.gemini as _gemini
    import voidx.llm.providers.kimi as _kimi
    import voidx.llm.providers.longcat as _longcat
    import voidx.llm.providers.mimo as _mimo
    import voidx.llm.providers.minimax as _minimax
    import voidx.llm.providers.openai as _openai
    import voidx.llm.providers.openrouter as _openrouter
    import voidx.llm.providers.qwen as _qwen
    import voidx.llm.providers.typex as _typex
    import voidx.llm.providers.xunfei as _xunfei
    import voidx.llm.providers.zhipu as _zhipu

    _ = (
        _anthropic,
        _deepseek,
        _doubao,
        _gemini,
        _kimi,
        _longcat,
        _mimo,
        _minimax,
        _openai,
        _openrouter,
        _qwen,
        _typex,
        _xunfei,
        _zhipu,
    )
