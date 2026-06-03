"""LLM provider shim — delegating to voidx_core."""

# The Rust engine handles model creation internally.
# These are compatibility stubs for the slash command system.

def create_chat_model(api_key: str, config):
    """No-op — the Rust agent creates its own client."""
    return None


def resolve_protocol(config):
    """Return protocol for a model config."""
    provider = getattr(config, "provider", "openai")
    explicit = getattr(config, "protocol", None)
    if explicit:
        return explicit
    return _PROTOCOLS.get(provider, "openai")


def get_context_limit(provider: str, protocol: str = "") -> int:
    """Return context window limit for a provider."""
    limits = {
        "deepseek": 1_000_000,
        "anthropic": 200_000,
        "openai": 1_050_000,
        "openrouter": 128_000,
        "mimo": 1_000_000,
        "qwen": 1_000_000,
        "zhipu": 200_000,
        "kimi": 262_144,
        "doubao": 256_000,
    }
    return limits.get(provider, 128_000)


_PROTOCOLS = {
    "anthropic": "anthropic",
    "deepseek": "anthropic",
    "openai": "openai",
    "openrouter": "openai",
    "mimo": "anthropic",
    "qwen": "anthropic",
    "zhipu": "anthropic",
    "kimi": "anthropic",
    "doubao": "openai",
}
