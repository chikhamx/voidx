"""Shim — lightweight replacements for token/usage formatting.

The heavy lifting (token counting, LLM calls) is done in Rust via voidx_core.
These are pure-Python formatting helpers for the TUI.
"""


class UsageStats:
    """Thin placeholder — real tracking is in Rust."""

    def __init__(self, context_limit: int = 200_000):
        self.context_limit = context_limit
        self.total_input = 0
        self.total_output = 0
        self.call_count = 0

    def record_call(self, usage=None, fallback_input=0, fallback_output=0):
        self.call_count += 1
        if usage:
            self.total_input += usage.get("input_tokens", fallback_input)
            self.total_output += usage.get("output_tokens", fallback_output)
        else:
            self.total_input += fallback_input
            self.total_output += fallback_output


def format_token_count(count: int) -> str:
    """Human-readable token count."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def format_cache_hit_rate(hit: int, total: int) -> str:
    """Cache hit percentage string."""
    if total == 0:
        return "n/a"
    return f"{(hit / total) * 100:.1f}%"


def estimate_tokens(text: str) -> int:
    """Rough token estimate: 4 chars ≈ 1 token."""
    return max(1, len(text) // 4)
