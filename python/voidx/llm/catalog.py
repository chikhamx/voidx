"""Shim — model catalog backed by Rust voidx_core."""

# Provider model lists come from Rust
try:
    import voidx_core

    def list_models(provider: str) -> list[str]:
        return voidx_core.RustAgent.list_models(provider)

except ImportError:
    # Fallback for when voidx_core is not built yet
    _STATIC = {
        "anthropic": ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"],
        "openai": ["gpt-5.5", "gpt-5.4-mini", "o3", "o4-mini"],
        "deepseek": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "mimo": ["mimo-v2.5-pro", "mimo-v2.5"],
        "qwen": ["qwen3.7-max", "qwen3-max"],
        "zhipu": ["glm-5.1", "glm-5"],
        "kimi": ["kimi-k2.6", "kimi-k2.5"],
    }

    def list_models(provider: str) -> list[str]:
        return _STATIC.get(provider, [])


_settings = None


def bind_settings(settings) -> None:
    """No-op shim — settings merging handled by Rust config."""
    global _settings
    _settings = settings
