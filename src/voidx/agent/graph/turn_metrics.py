"""Dedicated metrics sink for turn control protocol health.

Separate from ``_usage_stats`` to keep turn-control protocol metrics
independent from token/cost accounting.
"""

from __future__ import annotations

_COUNTER_NAMES = (
    "turn_control_called",
    "turn_control_missing",
    "turn_control_invalid",
    "turn_control_mixed_tools",
    "turn_control_first_prompt",
    "turn_control_second_prompt",
    "turn_control_prompt_succeeded",
    "turn_control_third_miss_fallback",
)


class TurnControlMetrics:
    def __init__(self) -> None:
        self._counters: dict[str, int] = {name: 0 for name in _COUNTER_NAMES}

    def increment(self, name: str, count: int = 1) -> None:
        if name in self._counters:
            self._counters[name] += count

    def snapshot(self) -> dict[str, int]:
        return dict(self._counters)

    def reset(self) -> None:
        for name in self._counters:
            self._counters[name] = 0
